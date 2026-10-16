package com.payguard.service

import com.payguard.db.Database
import com.payguard.db.Ledger
import com.payguard.domain.AuthorizeRequest
import com.payguard.domain.AuthorizationEvent
import com.payguard.domain.CaptureRequest
import com.payguard.domain.ControlContext
import com.payguard.domain.ControlEvaluator
import com.payguard.domain.FxRate
import com.payguard.domain.MoneyMinor
import com.payguard.domain.OperationResult
import com.payguard.domain.RefundRequest
import com.payguard.domain.ReverseRequest
import com.payguard.domain.SpendControls
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import java.sql.Connection
import java.time.Clock
import java.time.LocalDate
import java.time.ZoneOffset
import java.util.UUID

@Serializable
data class AuthorizationView(
    val id: String, val cardId: String, val merchantId: String, val amountMinor: Long,
    val currency: String, val billingMinor: Long, val status: String, val capturedBillingMinor: Long,
)

private data class AuthRow(
    val cardId: String, val merchantId: String, val currency: String, val amount: Long,
    val billing: Long, val fx: FxRate, val capturedAmount: Long, val capturedBilling: Long,
    val status: String, val billingCurrency: String,
)

class PaymentService(
    private val database: Database,
    private val cards: CardService,
    private val clock: Clock = Clock.systemUTC(),
    private val fraudScorer: FraudScorer = HttpFraudScorer(),
    private val modelMode: String = System.getenv("PAYGUARD_MODEL_MODE") ?: "off",
) {
    private val json = Json { encodeDefaults = true }

    init {
        require(modelMode in setOf("off", "shadow", "enforce"))
    }

    fun recentEvents(): List<AuthorizationEvent> = database.transaction { connection ->
        connection.prepareStatement(
            """SELECT payload::text FROM outbox WHERE topic='payguard.authorization'
                AND discarded_at IS NULL ORDER BY created_at DESC LIMIT 100"""
        ).use { statement ->
            statement.executeQuery().use { result ->
                buildList {
                    while (result.next()) {
                        add(json.decodeFromString<AuthorizationEvent>(result.getString(1)))
                    }
                }
            }
        }
    }

    fun authorize(request: AuthorizeRequest): OperationResult {
        val started = System.nanoTime()
        try {
        val result = database.transaction { connection ->
        once(connection, request.idempotencyKey, "authorize", json.encodeToString(request)) {
            require(request.authId.startsWith("auth_") && request.cardId.startsWith("card_"))
            require(request.merchantId.isNotBlank() && request.mcc.matches(Regex("[0-9]{4}")))
            require(request.country.matches(Regex("[A-Z]{2}")))
            require(request.latitude.isFinite() && request.latitude in -90.0..90.0)
            require(request.longitude.isFinite() && request.longitude in -180.0..180.0)
            require(request.homeLatitude.isFinite() && request.homeLatitude in -90.0..90.0)
            require(request.homeLongitude.isFinite() && request.homeLongitude in -180.0..180.0)
            require(request.merchantRisk.isFinite() && request.merchantRisk in 0.0..1.0)
            require(request.priorRefundRatio.isFinite() && request.priorRefundRatio in 0.0..1.0)
            val original = MoneyMinor(request.amountMinor, request.currency).requirePositive()
            val card = connection.prepareStatement(
                "SELECT company_id,state,currency,limit_minor FROM cards WHERE id=? FOR UPDATE"
            ).use {
                it.setString(1, request.cardId)
                it.executeQuery().use { result ->
                    require(result.next()) { "unknown card" }
                    listOf(result.getString(1), result.getString(2), result.getString(3).trim(), result.getLong(4).toString())
                }
            }
            val company = card[0]
            val billingCurrency = card[2]
            val fx = rate(connection, original.currency, billingCurrency)
            val billing = fx.convert(original, billingCurrency)
            val controls = connection.prepareStatement("SELECT policy::text FROM controls WHERE card_id=?").use {
                it.setString(1, request.cardId)
                it.executeQuery().use { result ->
                    require(result.next()) { "missing card controls" }
                    json.decodeFromString<SpendControls>(result.getString(1))
                }
            }
            // Card locks serialize its own limits; company limits need one lock shared
            // by all cards in the company under READ COMMITTED isolation.
            if (controls.companyDailyMinor != null || controls.companyMonthlyMinor != null) {
                connection.prepareStatement("SELECT id FROM companies WHERE id=? FOR UPDATE").use {
                    it.setString(1, company)
                    it.executeQuery().use { result -> check(result.next()) }
                }
            }
            val reasons = mutableListOf<String>()
            if (card[1] != "ACTIVE") reasons += "card_inactive"
            val context = controlContext(connection, request, company, billing.minor, controls)
            reasons += ControlEvaluator.declines(controls, context)
            if (billing.minor > card[3].toLong() || billing.minor > cards.available(connection, request.cardId)) {
                reasons += "insufficient_available"
            }
            val assessment = if (modelMode == "off" || card[1] != "ACTIVE") null else
                fraudScorer.score(request)
            if (modelMode == "enforce" && assessment?.wouldDecline == true) {
                reasons += "fraud_score"
            }
            val status = if (reasons.isEmpty()) "APPROVED" else "DECLINED"
            if (status == "APPROVED") {
                connection.prepareStatement(
                    """INSERT INTO authorizations(id,card_id,merchant_id,mcc,country,currency,
                       amount_minor,billing_minor,fx_numerator,fx_denominator,status)
                       VALUES (?,?,?,?,?,?,?,?,?,?,'OPEN')"""
                ).use {
                    it.setString(1, request.authId)
                    it.setString(2, request.cardId)
                    it.setString(3, request.merchantId)
                    it.setString(4, request.mcc)
                    it.setString(5, request.country)
                    it.setString(6, original.currency)
                    it.setLong(7, original.minor)
                    it.setLong(8, billing.minor)
                    it.setLong(9, fx.numerator)
                    it.setLong(10, fx.denominator)
                    it.executeUpdate()
                }
            }
            connection.prepareStatement(
                "INSERT INTO auth_attempts(card_id,merchant_id,amount_billing_minor,decision) VALUES (?,?,?,?)"
            ).use {
                it.setString(1, request.cardId)
                it.setString(2, request.merchantId)
                it.setLong(3, billing.minor)
                it.setString(4, status)
                it.executeUpdate()
            }
            val action = when {
                modelMode == "off" -> "OFF"
                assessment == null -> "RULES_FALLBACK"
                modelMode == "shadow" -> if (assessment.wouldDecline) "SHADOW_DECLINE" else "SHADOW_APPROVE"
                assessment.wouldDecline -> "MODEL_DECLINE"
                else -> "MODEL_APPROVE"
            }
            val result = OperationResult(status, request.authId, billing.minor, billingCurrency, reasons,
                assessment?.score, action, assessment?.reasonCodes ?: emptyList(),
                assessment?.modelVersion)
            outbox(connection, "payguard.authorization", request.cardId,
                json.encodeToString(AuthorizationEvent(request, result, clock.instant().toString())))
            result
        }
        }
        AuthMetrics.record(result, (System.nanoTime() - started) / 1_000_000_000.0)
        return result
        } catch (error: Exception) {
            AuthMetrics.recordError((System.nanoTime() - started) / 1_000_000_000.0)
            throw error
        }
    }

    fun capture(request: CaptureRequest): OperationResult = database.transaction { connection ->
        once(connection, request.idempotencyKey, "capture", json.encodeToString(request)) {
            require(request.captureId.startsWith("cap_") && request.amountMinor > 0)
            val auth = authRow(connection, request.authId)
            require(auth.status == "OPEN" && request.amountMinor <= auth.amount - auth.capturedAmount) { "invalid capture" }
            val final = request.amountMinor == auth.amount - auth.capturedAmount
            val billing = if (final) auth.billing - auth.capturedBilling else
                auth.fx.convert(MoneyMinor(request.amountMinor, auth.currency), auth.billingCurrency).minor
            require(billing > 0 && billing <= auth.billing - auth.capturedBilling)
            Ledger.post(connection, "capture", request.captureId, auth.billingCurrency, listOf(
                "card:${auth.cardId}:funding" to -billing,
                "merchant:${auth.merchantId}:clearing:${auth.billingCurrency}" to billing,
            ))
            connection.prepareStatement("INSERT INTO captures(id,auth_id,amount_minor,billing_minor) VALUES (?,?,?,?)").use {
                it.setString(1, request.captureId)
                it.setString(2, request.authId)
                it.setLong(3, request.amountMinor)
                it.setLong(4, billing)
                it.executeUpdate()
            }
            connection.prepareStatement(
                "UPDATE authorizations SET captured_amount_minor=captured_amount_minor+?, " +
                    "captured_billing_minor=captured_billing_minor+?, status=? WHERE id=?"
            ).use {
                it.setLong(1, request.amountMinor)
                it.setLong(2, billing)
                it.setString(3, if (final) "CLOSED" else "OPEN")
                it.setString(4, request.authId)
                it.executeUpdate()
            }
            OperationResult("CAPTURED", request.captureId, billing, auth.billingCurrency)
        }
    }

    fun reverse(request: ReverseRequest): OperationResult = database.transaction { connection ->
        once(connection, request.idempotencyKey, "reverse", json.encodeToString(request)) {
            val auth = authRow(connection, request.authId)
            require(auth.status == "OPEN") { "authorization has no open hold" }
            connection.prepareStatement("UPDATE authorizations SET status='REVERSED' WHERE id=?").use {
                it.setString(1, request.authId)
                it.executeUpdate()
            }
            OperationResult("REVERSED", request.authId)
        }
    }

    fun refund(request: RefundRequest): OperationResult = database.transaction { connection ->
        once(connection, request.idempotencyKey, "refund", json.encodeToString(request)) {
            require(request.refundId.startsWith("ref_") && request.amountMinor > 0)
            val capture = connection.prepareStatement(
                """SELECT c.auth_id,c.amount_minor,c.billing_minor,a.card_id,a.merchant_id,a.currency,
                    card.currency,a.fx_numerator,a.fx_denominator FROM captures c
                    JOIN authorizations a ON a.id=c.auth_id JOIN cards card ON card.id=a.card_id
                    WHERE c.id=? FOR UPDATE OF c"""
            ).use {
                it.setString(1, request.captureId)
                it.executeQuery().use { result ->
                    require(result.next()) { "unknown capture" }
                    listOf(result.getString(1),result.getString(2),result.getString(3),result.getString(4),
                        result.getString(5),result.getString(6),result.getString(7),result.getString(8),result.getString(9))
                }
            }
            val refunded = connection.prepareStatement(
                "SELECT coalesce(sum(amount_minor),0),coalesce(sum(billing_minor),0) FROM refunds WHERE capture_id=?"
            ).use {
                it.setString(1, request.captureId)
                it.executeQuery().use { result ->
                    result.next()
                    result.getBigDecimal(1).longValueExact() to result.getBigDecimal(2).longValueExact()
                }
            }
            val capturedOriginal = capture[1].toLong()
            val capturedBilling = capture[2].toLong()
            require(request.amountMinor <= capturedOriginal - refunded.first) { "refund exceeds captured amount" }
            val final = request.amountMinor == capturedOriginal - refunded.first
            val billing = if (final) capturedBilling - refunded.second else
                FxRate(capture[7].toLong(), capture[8].toLong())
                    .convert(MoneyMinor(request.amountMinor, capture[5].trim()), capture[6].trim()).minor
            require(billing > 0 && billing <= capturedBilling - refunded.second)
            Ledger.post(connection, "refund", request.refundId, capture[6].trim(), listOf(
                "card:${capture[3]}:funding" to billing,
                "merchant:${capture[4]}:clearing:${capture[6].trim()}" to -billing,
            ))
            connection.prepareStatement("INSERT INTO refunds(id,capture_id,amount_minor,billing_minor) VALUES (?,?,?,?)").use {
                it.setString(1, request.refundId)
                it.setString(2, request.captureId)
                it.setLong(3, request.amountMinor)
                it.setLong(4, billing)
                it.executeUpdate()
            }
            OperationResult("REFUNDED", request.refundId, billing, capture[6].trim())
        }
    }

    fun transactions(cardId: String): List<AuthorizationView> = database.transaction { connection ->
        connection.prepareStatement(
            """SELECT a.id,a.card_id,a.merchant_id,a.amount_minor,a.currency,a.billing_minor,
                a.status,a.captured_billing_minor FROM authorizations a WHERE card_id=? ORDER BY created_at DESC LIMIT 100"""
        ).use {
            it.setString(1, cardId)
            it.executeQuery().use { result ->
                buildList {
                    while (result.next()) add(AuthorizationView(result.getString(1),result.getString(2),
                        result.getString(3),result.getLong(4),result.getString(5).trim(),result.getLong(6),
                        result.getString(7),result.getLong(8)))
                }
            }
        }
    }

    private fun authRow(connection: Connection, authId: String): AuthRow = connection.prepareStatement(
        """SELECT a.card_id,a.merchant_id,a.currency,a.amount_minor,a.billing_minor,a.fx_numerator,
            a.fx_denominator,a.captured_amount_minor,a.captured_billing_minor,a.status,c.currency
            FROM authorizations a JOIN cards c ON c.id=a.card_id WHERE a.id=? FOR UPDATE OF a"""
    ).use {
        it.setString(1, authId)
        it.executeQuery().use { result ->
            require(result.next()) { "unknown authorization" }
            AuthRow(result.getString(1),result.getString(2),result.getString(3).trim(),result.getLong(4),
                result.getLong(5),FxRate(result.getLong(6),result.getLong(7)),result.getLong(8),
                result.getLong(9),result.getString(10),result.getString(11).trim())
        }
    }

    private fun rate(connection: Connection, source: String, target: String): FxRate {
        if (source == target) return FxRate(1, 1)
        return connection.prepareStatement(
            "SELECT numerator,denominator FROM fx_rates WHERE source_currency=? AND target_currency=?"
        ).use {
            it.setString(1, source)
            it.setString(2, target)
            it.executeQuery().use { result ->
                require(result.next()) { "unsupported FX pair $source/$target" }
                FxRate(result.getLong(1), result.getLong(2))
            }
        }
    }

    private fun controlContext(connection: Connection, request: AuthorizeRequest, company: String,
                               billing: Long, policy: SpendControls): ControlContext {
        val now = clock.instant()
        val day = LocalDate.ofInstant(now, ZoneOffset.UTC).atStartOfDay().toInstant(ZoneOffset.UTC)
        val month = LocalDate.ofInstant(now, ZoneOffset.UTC).withDayOfMonth(1).atStartOfDay().toInstant(ZoneOffset.UTC)
        fun spent(cardOnly: Boolean, since: java.time.Instant): Long = connection.prepareStatement(
            if (cardOnly) "SELECT coalesce(sum(amount_billing_minor),0) FROM auth_attempts " +
                "WHERE card_id=? AND decision='APPROVED' AND created_at>=?" else
                "SELECT coalesce(sum(at.amount_billing_minor),0) FROM auth_attempts at " +
                    "JOIN cards c ON c.id=at.card_id WHERE c.company_id=? AND at.decision='APPROVED' AND at.created_at>=?"
        ).use {
            it.setString(1, if (cardOnly) request.cardId else company)
            it.setObject(2, java.time.OffsetDateTime.ofInstant(since, ZoneOffset.UTC))
            it.executeQuery().use { result -> result.next(); result.getBigDecimal(1).longValueExact() }
        }
        val recent = connection.prepareStatement(
            "SELECT count(*) FROM auth_attempts WHERE card_id=? AND created_at>=?"
        ).use {
            it.setString(1, request.cardId)
            it.setObject(2, java.time.OffsetDateTime.ofInstant(now.minusSeconds(policy.velocitySeconds.toLong()), ZoneOffset.UTC))
            it.executeQuery().use { result -> result.next(); result.getInt(1) }
        }
        return ControlContext(billing, request.mcc, request.merchantId, request.country,
            if (policy.cardDailyMinor != null) spent(true, day) else 0,
            if (policy.cardMonthlyMinor != null) spent(true, month) else 0,
            if (policy.companyDailyMinor != null) spent(false, day) else 0,
            if (policy.companyMonthlyMinor != null) spent(false, month) else 0,
            recent)
    }

    private fun outbox(connection: Connection, topic: String, key: String, payload: String) {
        connection.prepareStatement("INSERT INTO outbox(id,topic,event_key,payload) VALUES (?,?,?,?::jsonb)").use {
            it.setObject(1, UUID.randomUUID())
            it.setString(2, topic)
            it.setString(3, key)
            it.setString(4, payload)
            it.executeUpdate()
        }
    }
}
