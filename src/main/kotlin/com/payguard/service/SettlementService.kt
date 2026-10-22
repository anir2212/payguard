package com.payguard.service

import com.payguard.db.Database
import kotlinx.serialization.Serializable
import java.time.LocalDate

@Serializable
data class SettlementItem(
    val captureId: String,
    val status: String,
    val expectedMinor: Long? = null,
    val observedMinor: Long? = null,
    val expectedCurrency: String? = null,
    val observedCurrency: String? = null,
)

@Serializable
data class SettlementReport(
    val settlementDate: String,
    val matched: Int,
    val missingNetwork: Int,
    val missingLedger: Int,
    val amountMismatch: Int,
    val duplicate: Int,
    val items: List<SettlementItem>,
    /** Capture-to-journal audit for the same UTC date; independent of the network file. */
    val journalIntegrity: JournalIntegrityReport,
)

/**
 * One capture record compared with its `kind='capture'` journals. `postedMinor` is the merchant-clearing
 * credit summed over journals in the capture's billing currency; journals in another currency are listed in
 * `postedCurrencies` but never counted as posted money for the capture.
 */
@Serializable
data class JournalIntegrityItem(
    val captureId: String,
    val status: String,
    val captureMinor: Long,
    val captureCurrency: String,
    val journalCount: Int,
    val postedMinor: Long,
    val postedCurrencies: List<String>,
)

@Serializable
data class JournalIntegrityReport(
    val settlementDate: String,
    val capturesChecked: Int,
    val posted: Int,
    val missingJournal: Int,
    val duplicateJournal: Int,
    val wrongCurrencyJournal: Int,
    val journalAmountMismatch: Int,
    val items: List<JournalIntegrityItem>,
)

private data class SettlementLine(val captureId: String, val amountMinor: Long, val currency: String)

private data class CaptureJournal(val currency: String, val merchantCreditMinor: Long)

class SettlementService(private val database: Database) {
    private fun utcRange(date: LocalDate) = Pair(
        date.atStartOfDay(java.time.ZoneOffset.UTC).toOffsetDateTime(),
        date.plusDays(1).atStartOfDay(java.time.ZoneOffset.UTC).toOffsetDateTime(),
    )

    private fun expected(date: LocalDate): Map<String, SettlementLine> = database.transaction { connection ->
        connection.prepareStatement(
            """SELECT c.id,sum(p.amount_minor),j.currency FROM captures c
                JOIN authorizations a ON a.id=c.auth_id JOIN cards card ON card.id=a.card_id
                JOIN journal_entries j ON j.kind='capture' AND j.reference=c.id
                JOIN postings p ON p.journal_id=j.id
                    AND p.account_id='merchant:' || a.merchant_id || ':clearing:' || card.currency
                WHERE c.created_at>=? AND c.created_at<?
                GROUP BY c.id,j.currency ORDER BY c.id"""
        ).use { statement ->
            val (start, end) = utcRange(date)
            statement.setObject(1, start)
            statement.setObject(2, end)
            statement.executeQuery().use { result ->
                buildMap {
                    while (result.next()) {
                        val line = SettlementLine(result.getString(1), result.getBigDecimal(2).longValueExact(),
                            result.getString(3).trim())
                        put(line.captureId, line)
                    }
                }
            }
        }
    }

    /**
     * Checks every capture record created on the UTC date against its balanced capture journals, without
     * reading any network file. A capture missing from both the journal and the network CSV is invisible to
     * [reconcile]'s comparison but appears here as `MISSING_JOURNAL`. Statuses use a fixed precedence:
     * missing, duplicate, wrong currency, then amount mismatch against the capture's billed amount.
     */
    fun journalIntegrity(date: LocalDate): JournalIntegrityReport = database.transaction { connection ->
        connection.prepareStatement(
            """SELECT c.id,c.billing_minor,card.currency,j.id,j.currency,
                    coalesce(sum(p.amount_minor) FILTER (WHERE p.account_id=
                        'merchant:' || a.merchant_id || ':clearing:' || j.currency),0)
                FROM captures c
                JOIN authorizations a ON a.id=c.auth_id JOIN cards card ON card.id=a.card_id
                LEFT JOIN journal_entries j ON j.kind='capture' AND j.reference=c.id
                LEFT JOIN postings p ON p.journal_id=j.id
                WHERE c.created_at>=? AND c.created_at<?
                GROUP BY c.id,c.billing_minor,card.currency,j.id,j.currency ORDER BY c.id,j.id"""
        ).use { statement ->
            val (start, end) = utcRange(date)
            statement.setObject(1, start)
            statement.setObject(2, end)
            val captures = linkedMapOf<String, Triple<Long, String, MutableList<CaptureJournal>>>()
            statement.executeQuery().use { result ->
                while (result.next()) {
                    val entry = captures.getOrPut(result.getString(1)) {
                        Triple(result.getLong(2), result.getString(3).trim(), mutableListOf())
                    }
                    if (result.getObject(4) != null) {
                        entry.third += CaptureJournal(result.getString(5).trim(),
                            result.getBigDecimal(6).longValueExact())
                    }
                }
            }
            val items = captures.map { (id, entry) ->
                val (captureMinor, currency, journals) = entry
                val posted = journals.filter { it.currency == currency }.sumOf { it.merchantCreditMinor }
                val status = when {
                    journals.isEmpty() -> "MISSING_JOURNAL"
                    journals.size > 1 -> "DUPLICATE_JOURNAL"
                    journals.single().currency != currency -> "WRONG_CURRENCY_JOURNAL"
                    posted != captureMinor -> "JOURNAL_AMOUNT_MISMATCH"
                    else -> "JOURNAL_POSTED"
                }
                JournalIntegrityItem(id, status, captureMinor, currency, journals.size, posted,
                    journals.map { it.currency }.distinct())
            }
            JournalIntegrityReport(date.toString(), items.size,
                items.count { it.status == "JOURNAL_POSTED" },
                items.count { it.status == "MISSING_JOURNAL" },
                items.count { it.status == "DUPLICATE_JOURNAL" },
                items.count { it.status == "WRONG_CURRENCY_JOURNAL" },
                items.count { it.status == "JOURNAL_AMOUNT_MISMATCH" }, items)
        }
    }

    fun sample(date: LocalDate): String = buildString {
        appendLine("capture_id,amount_minor,currency")
        for (line in expected(date).values) {
            appendLine("${line.captureId},${line.amountMinor},${line.currency}")
        }
    }

    fun reconcile(date: LocalDate, csv: String): SettlementReport {
        val expected = expected(date)
        val rows = csv.lineSequence().map(String::trim).filter(String::isNotEmpty).toList()
        require(rows.firstOrNull() == "capture_id,amount_minor,currency") {
            "expected header capture_id,amount_minor,currency"
        }
        val seen = mutableSetOf<String>()
        val items = mutableListOf<SettlementItem>()
        for ((index, row) in rows.drop(1).withIndex()) {
            val parts = row.split(',')
            require(parts.size == 3) { "invalid settlement row ${index + 2}" }
            val id = parts[0].trim()
            val amount = parts[1].trim().toLongOrNull()
            val currency = parts[2].trim()
            require(id.startsWith("cap_") && id.all { it.isLetterOrDigit() || it == '_' || it == '-' }) {
                "invalid capture ID on row ${index + 2}"
            }
            require(amount != null && amount > 0 && currency.matches(Regex("[A-Z]{3}"))) {
                "invalid amount or currency on row ${index + 2}"
            }
            val ledger = expected[id]
            val status = when {
                !seen.add(id) -> "DUPLICATE"
                ledger == null -> "MISSING_LEDGER"
                amount != ledger.amountMinor || currency != ledger.currency -> "AMOUNT_MISMATCH"
                else -> "MATCHED"
            }
            items += SettlementItem(id, status, ledger?.amountMinor, amount,
                ledger?.currency, currency)
        }
        for ((id, ledger) in expected) {
            if (id !in seen) items += SettlementItem(id, "MISSING_NETWORK",
                ledger.amountMinor, expectedCurrency = ledger.currency)
        }
        return SettlementReport(date.toString(),
            items.count { it.status == "MATCHED" },
            items.count { it.status == "MISSING_NETWORK" },
            items.count { it.status == "MISSING_LEDGER" },
            items.count { it.status == "AMOUNT_MISMATCH" },
            items.count { it.status == "DUPLICATE" }, items, journalIntegrity(date))
    }
}
