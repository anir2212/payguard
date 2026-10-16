package com.payguard.service

import com.payguard.domain.OperationResult
import kotlinx.serialization.json.Json
import java.security.MessageDigest
import java.sql.Connection

private val json = Json { encodeDefaults = true }

/** One request key has one operation and one exact canonical request body. */
internal fun once(
    connection: Connection,
    key: String,
    operation: String,
    requestJson: String,
    work: () -> OperationResult,
): OperationResult {
    require(key.length in 8..128) { "idempotency key must have 8-128 characters" }
    val hash = MessageDigest.getInstance("SHA-256").digest(requestJson.toByteArray())
        .joinToString("") { "%02x".format(it) }
    connection.prepareStatement(
        "SELECT operation,request_hash,response::text FROM idempotency WHERE request_key=? FOR UPDATE"
    ).use { statement ->
        statement.setString(1, key)
        statement.executeQuery().use { result ->
            if (result.next()) {
                require(result.getString(1) == operation && result.getString(2) == hash) {
                    "idempotency key reused for a different request"
                }
                return json.decodeFromString<OperationResult>(result.getString(3))
            }
        }
    }
    val response = work()
    connection.prepareStatement(
        "INSERT INTO idempotency(request_key,operation,request_hash,response) VALUES (?,?,?,?::jsonb)"
    ).use { statement ->
        statement.setString(1, key)
        statement.setString(2, operation)
        statement.setString(3, hash)
        statement.setString(4, json.encodeToString(response))
        statement.executeUpdate()
    }
    return response
}
