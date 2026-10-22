package com.payguard

import com.payguard.db.Database
import com.payguard.db.Ledger
import com.payguard.domain.AuthorizeRequest
import com.payguard.domain.CaptureRequest
import com.payguard.domain.CardState
import com.payguard.domain.ChangeStateRequest
import com.payguard.domain.IssueCardRequest
import com.payguard.domain.SetControlsRequest
import com.payguard.domain.SpendControls
import com.payguard.service.CardService
import com.payguard.service.JournalIntegrityItem
import com.payguard.service.PaymentService
import com.payguard.service.SettlementItem
import com.payguard.service.SettlementService
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.jupiter.api.AfterAll
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.BeforeAll
import org.junit.jupiter.api.Test
import org.testcontainers.containers.PostgreSQLContainer
import java.io.File
import java.time.LocalDate
import java.time.ZoneOffset
import java.util.UUID

@Serializable
data class FixtureNetworkRow(val networkRow: Int, val captureId: String, val status: String,
    val expectedMinor: Long? = null, val observedMinor: Long? = null)

@Serializable
data class FixtureReconcilerOutput(
    val fixtureId: String,
    val settlementDate: String,
    val networkRows: List<FixtureNetworkRow>,
    val missingNetwork: List<SettlementItem>,
    val journal: List<JournalIntegrityItem>,
)

/**
 * Harness for the synthetic settlement fixture in `bench/settlement_fixture/`. It builds the issuer-side
 * ledger from `ledger_plan.csv`, feeds `network.csv` to the reconciler, and writes what the reconciler and
 * journal audit reported to `build/settlement-fixture/reconciler_output.json`. It deliberately never opens
 * `labels.csv`; `python -m payguard_fraud.settlement_fixture score` compares the output with the labels.
 */
class SettlementFixtureTest {
    companion object {
        private val postgres = PostgreSQLContainer<Nothing>("postgres:16").apply {
            withDatabaseName("payguard")
            withUsername("payguard")
            withPassword("local-demo-only")
        }
        private lateinit var database: Database

        @BeforeAll
        @JvmStatic
        fun start() {
            postgres.start()
            database = Database(postgres.jdbcUrl, postgres.username, postgres.password)
        }

        @AfterAll
        @JvmStatic
        fun stop() {
            database.close()
            postgres.stop()
        }
    }

    private val fixture = File("bench/settlement_fixture")
    private fun key() = "key_${UUID.randomUUID()}"

    private fun insertCaptureRow(captureId: String, authId: String, amount: Long) {
        database.transaction { connection ->
            connection.prepareStatement(
                "INSERT INTO captures(id,auth_id,amount_minor,billing_minor) VALUES (?,?,?,?)"
            ).use {
                it.setString(1, captureId)
                it.setString(2, authId)
                it.setLong(3, amount)
                it.setLong(4, amount)
                it.executeUpdate()
            }
        }
    }

    @Test
    fun runSyntheticFixtureThroughReconcilerWithoutReadingLabels() {
        val provenance = Json.parseToJsonElement(File(fixture, "provenance.json").readText()).jsonObject
        val date = LocalDate.parse(provenance.getValue("settlementDate").jsonPrimitive.content)
        val cards = CardService(database)
        val payments = PaymentService(database, cards)
        val card = cards.issue(IssueCardRequest("company_fixture", "Fixture Holder", "SGD", 10_000_000, key()))
            .reference
        cards.changeState(card, ChangeStateRequest(CardState.ACTIVE, key()))
        cards.setControls(card, SetControlsRequest(SpendControls(velocityCount = 100_000), key()))

        val plan = File(fixture, "ledger_plan.csv").readLines().filter(String::isNotBlank)
        assertEquals("capture_id,merchant_id,amount_minor,journal_fault,journal_amount_minor", plan.first())
        val captureIds = plan.drop(1).map { line ->
            val (captureId, merchant, amountText, fault, journalAmountText) = line.split(',')
            val amount = amountText.toLong()
            val auth = "auth_${captureId.removePrefix("cap_")}"
            assertEquals("APPROVED", payments.authorize(AuthorizeRequest(
                auth, card, merchant, "5411", "SG", "SGD", amount, idempotencyKey = key(),
            )).status)
            when (fault) {
                "none" -> payments.capture(CaptureRequest(captureId, auth, amount, key()))
                "duplicate" -> {
                    payments.capture(CaptureRequest(captureId, auth, amount, key()))
                    database.transaction { connection ->
                        Ledger.post(connection, "capture", captureId, "SGD", listOf(
                            "card:$card:funding" to -amount, "merchant:$merchant:clearing:SGD" to amount))
                    }
                }
                "missing" -> insertCaptureRow(captureId, auth, amount)
                "wrong_currency" -> {
                    insertCaptureRow(captureId, auth, amount)
                    database.transaction { connection ->
                        Ledger.post(connection, "capture", captureId, "USD", listOf(
                            "bank:fixture:USD" to -amount, "merchant:$merchant:clearing:USD" to amount))
                    }
                }
                "amount_off" -> {
                    val posted = journalAmountText.toLong()
                    insertCaptureRow(captureId, auth, amount)
                    database.transaction { connection ->
                        Ledger.post(connection, "capture", captureId, "SGD", listOf(
                            "card:$card:funding" to -posted, "merchant:$merchant:clearing:SGD" to posted))
                    }
                }
                else -> error("unknown journal fault $fault")
            }
            captureId
        }
        // The fixture's booking date: capture rows are moved to it so the UTC-day queries see only fixture rows.
        database.transaction { connection ->
            connection.prepareStatement("UPDATE captures SET created_at=? WHERE id=?").use { statement ->
                for (id in captureIds) {
                    statement.setObject(1, date.atTime(12, 0).atOffset(ZoneOffset.UTC))
                    statement.setString(2, id)
                    statement.addBatch()
                }
                statement.executeBatch()
            }
        }

        val network = File(fixture, "network.csv").readText()
        val networkIds = network.lines().drop(1).filter(String::isNotBlank).map { it.substringBefore(',') }
        val report = SettlementService(database).reconcile(date, network)
        // The reconciler emits one item per network row in file order, then MISSING_NETWORK items.
        val rowItems = report.items.take(networkIds.size)
        assertEquals(networkIds, rowItems.map { it.captureId })
        val missingNetwork = report.items.drop(networkIds.size)
        assertTrue(missingNetwork.all { it.status == "MISSING_NETWORK" })
        assertEquals(captureIds.size, report.journalIntegrity.capturesChecked)

        val output = FixtureReconcilerOutput(
            provenance.getValue("fixtureId").jsonPrimitive.content, date.toString(),
            rowItems.mapIndexed { index, item ->
                FixtureNetworkRow(index + 1, item.captureId, item.status, item.expectedMinor, item.observedMinor)
            },
            missingNetwork, report.journalIntegrity.items,
        )
        val target = File("build/settlement-fixture/reconciler_output.json")
        target.parentFile.mkdirs()
        target.writeText(Json { prettyPrint = true }.encodeToString(FixtureReconcilerOutput.serializer(), output) +
            "\n")
    }
}
