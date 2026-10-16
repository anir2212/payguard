package com.payguard.db

import java.math.BigInteger
import java.sql.Connection
import java.util.UUID

/** Append-only balanced journals. PostgreSQL's deferred constraint trigger checks the invariant again. */
object Ledger {
    fun isBalanced(legs: List<Pair<String, Long>>): Boolean =
        legs.size >= 2 && legs.all { it.second != 0L } &&
            legs.fold(BigInteger.ZERO) { sum, (_, minor) -> sum + BigInteger.valueOf(minor) } == BigInteger.ZERO

    fun post(connection: Connection, kind: String, reference: String, currency: String, legs: List<Pair<String, Long>>): UUID {
        require(isBalanced(legs)) { "unbalanced journal" }
        val entryId = UUID.randomUUID()
        for ((account, _) in legs) {
            connection.prepareStatement("INSERT INTO accounts(id,currency) VALUES (?,?) ON CONFLICT (id) DO NOTHING").use {
                it.setString(1, account)
                it.setString(2, currency)
                it.executeUpdate()
            }
        }
        connection.prepareStatement("INSERT INTO journal_entries(id,kind,reference,currency) VALUES (?,?,?,?)").use {
            it.setObject(1, entryId)
            it.setString(2, kind)
            it.setString(3, reference)
            it.setString(4, currency)
            it.executeUpdate()
        }
        connection.prepareStatement("INSERT INTO postings(journal_id,account_id,amount_minor) VALUES (?,?,?)").use { statement ->
            for ((account, minor) in legs) {
                statement.setObject(1, entryId)
                statement.setString(2, account)
                statement.setLong(3, minor)
                statement.addBatch()
            }
            statement.executeBatch()
        }
        return entryId
    }

    fun balance(connection: Connection, account: String): Long = connection.prepareStatement(
        "SELECT coalesce(sum(amount_minor),0) FROM postings WHERE account_id=?"
    ).use { statement ->
        statement.setString(1, account)
        statement.executeQuery().use { result ->
            result.next()
            result.getBigDecimal(1).longValueExact()
        }
    }
}
