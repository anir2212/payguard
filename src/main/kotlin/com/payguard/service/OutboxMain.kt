package com.payguard.service

import com.payguard.db.Database
import org.apache.kafka.clients.producer.KafkaProducer
import org.apache.kafka.clients.producer.Producer
import org.apache.kafka.clients.producer.ProducerRecord
import org.apache.kafka.common.serialization.StringSerializer
import java.util.Properties
import java.util.UUID
import java.util.concurrent.TimeUnit

/** At-least-once outbox drain. A crash after broker ack can replay an event ID. */
class OutboxPublisher(
    private val database: Database,
    private val producer: Producer<String, String>,
) : AutoCloseable {
    constructor(database: Database, brokers: String) : this(database, kafkaProducer(brokers))

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
        if (rows.isEmpty()) return@transaction 0
        // Send the whole batch before waiting, so the producer can group records into
        // a few requests; rows are marked published only after every send is acked.
        val sends = rows.map { producer.send(ProducerRecord(it[1], it[2], it[3])) }
        producer.flush()
        sends.forEach { it.get(5, TimeUnit.SECONDS) }
        val ids = rows.map { UUID.fromString(it[0]) }.toTypedArray()
        connection.prepareStatement("UPDATE outbox SET published_at=now() WHERE id = ANY(?)").use {
            it.setArray(1, connection.createArrayOf("uuid", ids))
            it.executeUpdate()
        }
        rows.size
    }

    override fun close() = producer.close()
}

private fun kafkaProducer(brokers: String) = KafkaProducer<String, String>(Properties().apply {
    put("bootstrap.servers", brokers)
    put("key.serializer", StringSerializer::class.java.name)
    put("value.serializer", StringSerializer::class.java.name)
    put("acks", "all")
    put("enable.idempotence", "true")
    put("compression.type", "zstd")
})

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
