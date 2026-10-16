package com.payguard

import com.payguard.db.Database
import com.payguard.db.Ledger
import com.payguard.domain.AuthorizeRequest
import com.payguard.domain.CardState
import com.payguard.domain.CaptureRequest
import com.payguard.domain.ChangeStateRequest
import com.payguard.domain.IssueCardRequest
import com.payguard.domain.RefundRequest
import com.payguard.domain.ReverseRequest
import com.payguard.domain.SetControlsRequest
import com.payguard.domain.SpendControls
import com.payguard.service.CardService
import com.payguard.service.PaymentService
import com.payguard.service.SettlementService
import com.payguard.service.FraudScorer
import com.payguard.service.FraudAssessment
import org.junit.jupiter.api.AfterAll
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.BeforeAll
import org.junit.jupiter.api.Test
import org.testcontainers.containers.PostgreSQLContainer
import java.sql.SQLException
import java.time.LocalDate
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.UUID
import kotlin.random.Random

class PaymentIntegrationTest {
    companion object {
        private val postgres = PostgreSQLContainer<Nothing>("postgres:16").apply {
            withDatabaseName("payguard")
            withUsername("payguard")
            withPassword("local-demo-only")
        }
        private lateinit var database: Database
        private lateinit var cards: CardService
        private lateinit var payments: PaymentService

        @BeforeAll
        @JvmStatic
        fun start() {
            postgres.start()
            database = Database(postgres.jdbcUrl, postgres.username, postgres.password)
            cards = CardService(database)
            payments = PaymentService(database, cards)
        }

        @AfterAll
        @JvmStatic
        fun stop() {
            database.close()
            postgres.stop()
        }
    }

    private fun key() = "key_${UUID.randomUUID()}"

    private fun activeCard(limit: Long = 50_000, companyId: String = "company_test"): String {
        val issued = cards.issue(IssueCardRequest(companyId, "Test Holder", "SGD", limit, key()))
        cards.changeState(issued.reference, ChangeStateRequest(CardState.ACTIVE, key()))
        return issued.reference
    }

    @Test
    fun captureAndRefundPreserveBalancesAndReplaySafety() {
        val card = activeCard()
        val auth = "auth_${UUID.randomUUID()}"
        val request = AuthorizeRequest(auth, card, "shop", "5411", "SG", "SGD", 1_000, idempotencyKey = key())
        assertEquals("APPROVED", payments.authorize(request).status)
        assertEquals("APPROVED", payments.authorize(request).status)
        assertEquals(49_000, cards.card(card).availableMinor)
        val firstCapture = CaptureRequest("cap_${UUID.randomUUID()}", auth, 400, key())
        payments.capture(firstCapture)
        assertEquals(49_000, cards.card(card).availableMinor)
        payments.capture(CaptureRequest("cap_${UUID.randomUUID()}", auth, 600, key()))
        assertEquals(49_000, cards.card(card).availableMinor)
        payments.refund(RefundRequest("ref_${UUID.randomUUID()}", firstCapture.captureId, 400, key()))
        assertEquals(49_400, cards.card(card).availableMinor)
        assertThrows(IllegalArgumentException::class.java) {
            payments.authorize(request.copy(amountMinor = 2_000))
        }
        database.transaction { connection ->
            connection.createStatement().use { statement ->
                statement.executeQuery("SELECT count(*) FROM journal_entries j LEFT JOIN postings p " +
                    "ON p.journal_id=j.id GROUP BY j.id HAVING count(p.id)<2 OR sum(p.amount_minor)<>0").use {
                    assertTrue(!it.next())
                }
            }
        }
    }

    @Test
    fun reverseReleasesOnlyUncapturedHold() {
        val card = activeCard()
        val auth = "auth_${UUID.randomUUID()}"
        payments.authorize(AuthorizeRequest(auth, card, "shop", "5411", "SG", "SGD", 1_000, idempotencyKey = key()))
        payments.capture(CaptureRequest("cap_${UUID.randomUUID()}", auth, 400, key()))
        assertEquals(49_000, cards.card(card).availableMinor)
        payments.reverse(ReverseRequest(auth, key()))
        assertEquals(49_600, cards.card(card).availableMinor)
    }

    @Test
    fun databaseRejectsUnbalancedJournalAtCommit() {
        assertThrows(SQLException::class.java) {
            database.transaction { connection ->
                connection.prepareStatement("INSERT INTO journal_entries(id,kind,reference,currency) VALUES (?,'bad','bad','SGD')").use {
                    it.setObject(1, UUID.randomUUID())
                    it.executeUpdate()
                }
            }
        }
    }

    @Test
    fun randomCaptureRefundSequenceKeepsEveryJournalBalanced() {
        val card = activeCard(1_000_000)
        cards.setControls(card, SetControlsRequest(SpendControls(velocityCount = 1_000), key()))
        val random = Random(20261011)
        repeat(40) { index ->
            val amount = random.nextLong(100, 2_000)
            val auth = "auth_${UUID.randomUUID()}"
            assertEquals("APPROVED", payments.authorize(AuthorizeRequest(
                auth, card, "shop_$index", "5411", "SG", "SGD", amount,
                idempotencyKey = key(),
            )).status)
            val first = random.nextLong(1, amount)
            val capture = CaptureRequest("cap_${UUID.randomUUID()}", auth, first, key())
            payments.capture(capture)
            payments.capture(CaptureRequest("cap_${UUID.randomUUID()}", auth, amount - first, key()))
            if (index % 2 == 0) {
                payments.refund(RefundRequest("ref_${UUID.randomUUID()}", capture.captureId, first, key()))
            }
        }
        database.transaction { connection ->
            connection.createStatement().use { statement ->
                statement.executeQuery("SELECT count(*) FROM journal_entries j JOIN postings p " +
                    "ON p.journal_id=j.id GROUP BY j.id HAVING count(p.id)<2 OR sum(p.amount_minor)<>0").use {
                    assertTrue(!it.next())
                }
            }
        }
    }

    @Test
    fun scoringModesAndTimeoutFallbackHaveExplicitOutcomes() {
        val card = activeCard()
        val high = FraudScorer { FraudAssessment(0.99, true, listOf("velocity"), "test-model") }
        val shadow = PaymentService(database, cards, fraudScorer = high, modelMode = "shadow")
        val first = shadow.authorize(AuthorizeRequest("auth_${UUID.randomUUID()}", card, "shop",
            "5411", "SG", "SGD", 100, idempotencyKey = key()))
        assertEquals("APPROVED", first.status)
        assertEquals("SHADOW_DECLINE", first.modelAction)
        val enforced = PaymentService(database, cards, fraudScorer = high, modelMode = "enforce")
        val second = enforced.authorize(AuthorizeRequest("auth_${UUID.randomUUID()}", card, "shop",
            "5411", "SG", "SGD", 100, idempotencyKey = key()))
        assertEquals("DECLINED", second.status)
        assertTrue("fraud_score" in second.reasons)
        val unavailable = PaymentService(database, cards, fraudScorer = FraudScorer { null },
            modelMode = "enforce")
        val third = unavailable.authorize(AuthorizeRequest("auth_${UUID.randomUUID()}", card,
            "shop", "5411", "SG", "SGD", 100, idempotencyKey = key()))
        assertEquals("APPROVED", third.status)
        assertEquals("RULES_FALLBACK", third.modelAction)
    }

    @Test
    fun settlementReportSeparatesEveryDiscrepancy() {
        val card = activeCard()
        cards.setControls(card, SetControlsRequest(SpendControls(velocityCount = 1_000), key()))
        val captures = (0 until 3).map { index ->
            val auth = "auth_${UUID.randomUUID()}"
            assertEquals("APPROVED", payments.authorize(AuthorizeRequest(
                auth, card, "shop", "5411", "SG", "SGD", 100L + index,
                idempotencyKey = key(),
            )).status)
            val capture = "cap_${UUID.randomUUID()}"
            payments.capture(CaptureRequest(capture, auth, 100L + index, key()))
            capture
        }
        val service = SettlementService(database)
        val date = LocalDate.now(java.time.ZoneOffset.UTC)
        val sample = service.sample(date)
        assertTrue(sample.contains(captures[0]))
        val csv = """capture_id,amount_minor,currency
            ${captures[0]},100,SGD
            ${captures[0]},100,SGD
            ${captures[1]},999,SGD
            cap_unknown,50,SGD
        """.trimIndent()
        val report = service.reconcile(date, csv)
        assertEquals(1, report.matched)
        assertEquals(1, report.duplicate)
        assertEquals(1, report.amountMismatch)
        assertEquals(1, report.missingLedger)
        assertTrue(report.items.any { it.captureId == captures[2] && it.status == "MISSING_NETWORK" })
        assertTrue(report.missingNetwork >= 1)
    }

    @Test
    fun settlementUsesPostedMerchantLedgerAmountRatherThanCaptureRow() {
        val card = activeCard()
        val missingAuth = "auth_${UUID.randomUUID()}"
        val mismatchedAuth = "auth_${UUID.randomUUID()}"
        for ((auth, amount) in listOf(missingAuth to 500L, mismatchedAuth to 700L)) {
            assertEquals("APPROVED", payments.authorize(AuthorizeRequest(
                auth, card, "shop", "5411", "SG", "SGD", amount, idempotencyKey = key(),
            )).status)
        }
        val missingCapture = "cap_${UUID.randomUUID()}"
        val mismatchedCapture = "cap_${UUID.randomUUID()}"
        database.transaction { connection ->
            connection.prepareStatement(
                "INSERT INTO captures(id,auth_id,amount_minor,billing_minor) VALUES (?,?,?,?)"
            ).use { statement ->
                for ((capture, auth, amount) in listOf(
                    Triple(missingCapture, missingAuth, 500L),
                    Triple(mismatchedCapture, mismatchedAuth, 700L),
                )) {
                    statement.setString(1, capture)
                    statement.setString(2, auth)
                    statement.setLong(3, amount)
                    statement.setLong(4, amount)
                    statement.addBatch()
                }
                statement.executeBatch()
            }
            // Simulate a mismatched but balanced journal written outside the API transaction.
            Ledger.post(connection, "capture", mismatchedCapture, "SGD", listOf(
                "card:$card:funding" to -650L,
                "merchant:shop:clearing:SGD" to 650L,
            ))
        }
        val service = SettlementService(database)
        val date = LocalDate.now(java.time.ZoneOffset.UTC)
        val sample = service.sample(date)
        assertTrue(!sample.contains(missingCapture))
        assertTrue(sample.contains("$mismatchedCapture,650,SGD"))
        val report = service.reconcile(date, """capture_id,amount_minor,currency
            $missingCapture,500,SGD
            $mismatchedCapture,700,SGD
        """.trimIndent())
        assertTrue(report.items.any { it.captureId == missingCapture && it.status == "MISSING_LEDGER" })
        assertTrue(report.items.any { it.captureId == mismatchedCapture && it.status == "AMOUNT_MISMATCH" &&
            it.expectedMinor == 650L && it.observedMinor == 700L })
    }

    @Test
    fun companyLimitSerializesAcrossDifferentCards() {
        val company = "company_${UUID.randomUUID()}"
        val first = activeCard(companyId = company)
        val second = activeCard(companyId = company)
        val policy = SpendControls(companyDailyMinor = 150, velocityCount = 1_000)
        cards.setControls(first, SetControlsRequest(policy, key()))
        cards.setControls(second, SetControlsRequest(policy, key()))
        val gate = CountDownLatch(1)
        val pool = Executors.newFixedThreadPool(2)
        try {
            val attempts = listOf(first, second).map { card ->
                pool.submit<String> {
                    gate.await()
                    payments.authorize(AuthorizeRequest("auth_${UUID.randomUUID()}", card,
                        "shop", "5411", "SG", "SGD", 100, idempotencyKey = key())).status
                }
            }
            gate.countDown()
            assertEquals(setOf("APPROVED", "DECLINED"),
                attempts.map { it.get(10, TimeUnit.SECONDS) }.toSet())
        } finally {
            pool.shutdownNow()
        }
    }
}
