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
)

private data class SettlementLine(val captureId: String, val amountMinor: Long, val currency: String)

class SettlementService(private val database: Database) {
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
            statement.setObject(1, date.atStartOfDay(java.time.ZoneOffset.UTC).toOffsetDateTime())
            statement.setObject(2, date.plusDays(1).atStartOfDay(java.time.ZoneOffset.UTC).toOffsetDateTime())
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
            items.count { it.status == "DUPLICATE" }, items)
    }
}
