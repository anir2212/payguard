package com.payguard.service

import com.payguard.db.Database
import org.apache.kafka.clients.producer.KafkaProducer
import org.apache.kafka.clients.producer.ProducerRecord
import org.apache.kafka.common.serialization.StringSerializer
import java.util.Properties
import java.util.UUID
import java.util.concurrent.TimeUnit

/** At-least-once outbox drain. A crash after broker ack can replay an event ID. */
class OutboxPublisher(private val database: Database, brokers: String) : AutoCloseable {
    private val producer = KafkaProducer<String, String>(Properties().apply {
        put("bootstrap.servers", brokers)
        put("key.serializer", StringSerializer::class.java.name)
        put("value.serializer", StringSerializer::class.java.name)
        put("acks", "all")
        put("enable.idempotence", "true")
        put("compression.type", "zstd")
    })

    fun publishOnce(): Int = database.transaction { connection ->
        val rows = connection.prepareStatement(
            """SELECT id,topic,event_key,payload::text FROM outbox
               WHERE published_at IS NULL AND discarded_at IS NULL
               ORDER BY created_at,id LIMIT 100 FOR UPDATE SKIP LOCKED"""
        ).use { statement ->
            statement.executeQuery().use { result ->
                buildList {
                    while (result.next()) add(listOf(result.getString(1),result.getString(2),
                        result.getString(3),result.getString(4)))
                }
            }
        }
        for (row in rows) {
            producer.send(ProducerRecord(row[1], row[2], row[3])).get(5, TimeUnit.SECONDS)
            connection.prepareStatement("UPDATE outbox SET published_at=now() WHERE id=?").use {
                it.setObject(1, UUID.fromString(row[0]))
                it.executeUpdate()
            }
        }
        rows.size
    }

    override fun close() = producer.close()
}

fun main() {
    val brokers = System.getenv("PAYGUARD_KAFKA_BROKERS") ?: "127.0.0.1:19094"
    Database().use { database ->
        OutboxPublisher(database, brokers).use { publisher ->
            while (!Thread.currentThread().isInterrupted) {
                try {
                    if (publisher.publishOnce() == 0) Thread.sleep(100)
                } catch (error: Exception) {
                    System.err.println("Outbox publish retry: ${error.message}")
                    Thread.sleep(1_000)
                }
            }
        }
    }
}
