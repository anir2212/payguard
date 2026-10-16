package com.payguard.service

import com.payguard.domain.AuthorizeRequest
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import java.net.URI
import java.net.http.HttpClient
import java.net.http.HttpRequest
import java.net.http.HttpResponse
import java.time.Duration

@Serializable
data class FraudAssessment(
    val score: Double,
    val wouldDecline: Boolean,
    val reasonCodes: List<String>,
    val modelVersion: String,
)

fun interface FraudScorer {
    /** Null means unavailable; the caller must fall back to rules. */
    fun score(request: AuthorizeRequest): FraudAssessment?
}

class HttpFraudScorer(
    endpoint: String = System.getenv("PAYGUARD_SCORE_URL") ?: "http://127.0.0.1:18086/score",
) : FraudScorer {
    private val uri = URI.create(endpoint)
    private val client = HttpClient.newBuilder().connectTimeout(Duration.ofMillis(50)).build()
    private val json = Json { encodeDefaults = true; ignoreUnknownKeys = true }

    override fun score(request: AuthorizeRequest): FraudAssessment? {
        val httpRequest = HttpRequest.newBuilder(uri)
            .timeout(Duration.ofMillis(50))
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(json.encodeToString(request)))
            .build()
        return try {
            val response = client.send(httpRequest, HttpResponse.BodyHandlers.ofString())
            if (response.statusCode() != 200) null else
                json.decodeFromString<FraudAssessment>(response.body())
        } catch (error: InterruptedException) {
            Thread.currentThread().interrupt()
            null
        } catch (error: Exception) {
            null
        }
    }
}
