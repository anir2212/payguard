package com.payguard

import com.payguard.db.Database
import com.payguard.domain.AuthorizeRequest
import com.payguard.domain.CaptureRequest
import com.payguard.domain.ChangeStateRequest
import com.payguard.domain.IssueCardRequest
import com.payguard.domain.RefundRequest
import com.payguard.domain.ReverseRequest
import com.payguard.domain.SetControlsRequest
import com.payguard.service.CardService
import com.payguard.service.AuthMetrics
import com.payguard.service.PaymentService
import com.payguard.service.SettlementService
import io.ktor.http.HttpStatusCode
import io.ktor.http.ContentType
import io.ktor.serialization.kotlinx.json.json
import io.ktor.server.application.Application
import io.ktor.server.application.install
import io.ktor.server.engine.embeddedServer
import io.ktor.server.netty.Netty
import io.ktor.server.plugins.contentnegotiation.ContentNegotiation
import io.ktor.server.plugins.statuspages.StatusPages
import io.ktor.server.request.receive
import io.ktor.server.request.receiveText
import io.ktor.server.response.respond
import io.ktor.server.response.respondText
import io.ktor.server.routing.get
import io.ktor.server.routing.patch
import io.ktor.server.routing.post
import io.ktor.server.routing.put
import io.ktor.server.routing.route
import io.ktor.server.routing.routing
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.Serializable
import java.time.LocalDate

@Serializable
data class ErrorResponse(val error: String)

fun Application.payGuardModule(database: Database = Database()) {
    val cards = CardService(database)
    val payments = PaymentService(database, cards)
    val settlements = SettlementService(database)
    val appLog = environment.log
    install(ContentNegotiation) { json() }
    install(StatusPages) {
        exception<IllegalArgumentException> { call, cause ->
            call.respond(HttpStatusCode.BadRequest, ErrorResponse(cause.message ?: "invalid request"))
        }
        exception<Exception> { call, cause ->
            appLog.error("request failed", cause)
            call.respond(HttpStatusCode.InternalServerError, ErrorResponse("internal error"))
        }
    }
    routing {
        get("/health") { call.respond(mapOf("status" to "ok")) }
        get("/metrics") {
            call.respondText(AuthMetrics.render(), ContentType.parse("text/plain; version=0.0.4"))
        }
        get("/openapi.yaml") {
            val spec = checkNotNull(javaClass.getResource("/openapi.yaml")) { "OpenAPI file missing" }.readText()
            call.respondText(spec, ContentType.parse("application/yaml"))
        }
        route("/v1") {
            post("/cards") {
                val request = call.receive<IssueCardRequest>()
                call.respond(HttpStatusCode.Created, withContext(Dispatchers.IO) { cards.issue(request) })
            }
            get("/cards/{cardId}") {
                val cardId = requireNotNull(call.parameters["cardId"])
                call.respond(withContext(Dispatchers.IO) { cards.card(cardId) })
            }
            patch("/cards/{cardId}/state") {
                val cardId = requireNotNull(call.parameters["cardId"])
                val request = call.receive<ChangeStateRequest>()
                call.respond(withContext(Dispatchers.IO) { cards.changeState(cardId, request) })
            }
            put("/cards/{cardId}/controls") {
                val cardId = requireNotNull(call.parameters["cardId"])
                val request = call.receive<SetControlsRequest>()
                call.respond(withContext(Dispatchers.IO) { cards.setControls(cardId, request) })
            }
            get("/cards/{cardId}/transactions") {
                val cardId = requireNotNull(call.parameters["cardId"])
                call.respond(withContext(Dispatchers.IO) { payments.transactions(cardId) })
            }
            post("/authorizations") {
                val request = call.receive<AuthorizeRequest>()
                call.respond(withContext(Dispatchers.IO) { payments.authorize(request) })
            }
            get("/authorizations/recent") {
                call.respond(withContext(Dispatchers.IO) { payments.recentEvents() })
            }
            post("/captures") {
                val request = call.receive<CaptureRequest>()
                call.respond(withContext(Dispatchers.IO) { payments.capture(request) })
            }
            post("/reversals") {
                val request = call.receive<ReverseRequest>()
                call.respond(withContext(Dispatchers.IO) { payments.reverse(request) })
            }
            post("/refunds") {
                val request = call.receive<RefundRequest>()
                call.respond(withContext(Dispatchers.IO) { payments.refund(request) })
            }
            get("/settlements/sample") {
                val date = LocalDate.parse(requireNotNull(call.request.queryParameters["date"]))
                call.respondText(withContext(Dispatchers.IO) { settlements.sample(date) },
                    ContentType.parse("text/csv"))
            }
            get("/settlements/integrity") {
                val date = LocalDate.parse(requireNotNull(call.request.queryParameters["date"]))
                call.respond(withContext(Dispatchers.IO) { settlements.journalIntegrity(date) })
            }
            post("/settlements/reconcile") {
                val date = LocalDate.parse(requireNotNull(call.request.queryParameters["date"]))
                val csv = call.receiveText()
                call.respond(withContext(Dispatchers.IO) { settlements.reconcile(date, csv) })
            }
        }
    }
}

fun main() {
    val port = System.getenv("PAYGUARD_PORT")?.toIntOrNull() ?: 8086
    embeddedServer(Netty, port = port, host = "0.0.0.0") { payGuardModule() }.start(wait = true)
}
