package com.payguard.db

import com.zaxxer.hikari.HikariConfig
import com.zaxxer.hikari.HikariDataSource
import org.flywaydb.core.Flyway
import java.sql.Connection
import java.sql.SQLException
import java.util.concurrent.ThreadLocalRandom

class Database(
    jdbcUrl: String = System.getenv("PAYGUARD_JDBC_URL") ?: "jdbc:postgresql://127.0.0.1:15439/payguard",
    user: String = System.getenv("PAYGUARD_DB_USER") ?: "payguard",
    password: String = System.getenv("PAYGUARD_DB_PASSWORD") ?: "local-demo-only",
) : AutoCloseable {
    private val pool = HikariDataSource(HikariConfig().apply {
        this.jdbcUrl = jdbcUrl
        username = user
        this.password = password
        maximumPoolSize = 20
        minimumIdle = 1
        transactionIsolation = "TRANSACTION_READ_COMMITTED"
    })

    init {
        Flyway.configure().dataSource(pool).load().migrate()
    }

    fun <T> transaction(block: (Connection) -> T): T {
        var last: SQLException? = null
        repeat(8) { attempt ->
            pool.connection.use { connection ->
                // Every transaction uses READ COMMITTED, which the pool sets once per connection;
                // setting it again here cost PostgreSQL an extra SET on every transaction.
                connection.autoCommit = false
                try {
                    val result = block(connection)
                    connection.commit()
                    return result
                } catch (error: SQLException) {
                    connection.rollback()
                    if (error.sqlState !in setOf("40001", "40P01", "23505") || attempt == 7) throw error
                    last = error
                } catch (error: Exception) {
                    connection.rollback()
                    throw error
                }
            }
            Thread.sleep((1L shl attempt).coerceAtMost(32L) +
                ThreadLocalRandom.current().nextLong(0, 5))
        }
        throw last ?: IllegalStateException("transaction retry exhausted")
    }

    override fun close() = pool.close()
}
