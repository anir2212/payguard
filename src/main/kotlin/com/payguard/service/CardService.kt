package com.payguard.service

import com.payguard.db.Database
import com.payguard.db.Ledger
import com.payguard.domain.CURRENCY_CODE
import com.payguard.domain.CardLifecycle
import com.payguard.domain.CardState
import com.payguard.domain.CardView
import com.payguard.domain.ChangeStateRequest
import com.payguard.domain.IssueCardRequest
import com.payguard.domain.OperationResult
import com.payguard.domain.SetControlsRequest
import com.payguard.domain.SpendControls
import kotlinx.serialization.json.Json
import java.sql.Connection
import java.util.UUID

class CardService(private val database: Database) {
    private val json = Json { encodeDefaults = true }

    fun issue(request: IssueCardRequest): OperationResult = database.transaction { connection ->
        once(connection, request.idempotencyKey, "issue", json.encodeToString(request)) {
            require(request.companyId.isNotBlank() && request.holder.isNotBlank())
            require(request.limitMinor > 0)
            require(request.currency.matches(CURRENCY_CODE))
            val cardId = "card_${UUID.randomUUID()}"
            connection.prepareStatement("INSERT INTO companies(id) VALUES (?) ON CONFLICT DO NOTHING").use {
                it.setString(1, request.companyId)
                it.executeUpdate()
            }
            connection.prepareStatement(
                "INSERT INTO cards(id,company_id,holder,state,currency,limit_minor) VALUES (?,?,?,'CREATED',?,?)"
            ).use {
                it.setString(1, cardId)
                it.setString(2, request.companyId)
                it.setString(3, request.holder)
                it.setString(4, request.currency)
                it.setLong(5, request.limitMinor)
                it.executeUpdate()
            }
            connection.prepareStatement("INSERT INTO controls(card_id,policy) VALUES (?,?::jsonb)").use {
                it.setString(1, cardId)
                it.setString(2, json.encodeToString(SpendControls()))
                it.executeUpdate()
            }
            Ledger.post(connection, "fund", cardId, request.currency, listOf(
                "card:$cardId:funding" to request.limitMinor,
                "company:${request.companyId}:bank:${request.currency}" to -request.limitMinor,
            ))
            OperationResult("CREATED", cardId, request.limitMinor, request.currency)
        }
    }

    fun changeState(cardId: String, request: ChangeStateRequest): OperationResult = database.transaction { connection ->
        once(connection, request.idempotencyKey, "change_state", json.encodeToString(listOf(cardId, request.state.name))) {
            val current = connection.prepareStatement("SELECT state FROM cards WHERE id=? FOR UPDATE").use {
                it.setString(1, cardId)
                it.executeQuery().use { result ->
                    require(result.next()) { "unknown card" }
                    CardState.valueOf(result.getString(1))
                }
            }
            val next = CardLifecycle.transition(current, request.state)
            connection.prepareStatement("UPDATE cards SET state=? WHERE id=?").use {
                it.setString(1, next.name)
                it.setString(2, cardId)
                it.executeUpdate()
            }
            OperationResult(next.name, cardId)
        }
    }

    fun setControls(cardId: String, request: SetControlsRequest): OperationResult = database.transaction { connection ->
        once(connection, request.idempotencyKey, "set_controls", cardId + json.encodeToString(request.controls)) {
            connection.prepareStatement("SELECT id FROM cards WHERE id=? FOR UPDATE").use {
                it.setString(1, cardId)
                it.executeQuery().use { result -> require(result.next()) { "unknown card" } }
            }
            connection.prepareStatement("UPDATE controls SET policy=?::jsonb WHERE card_id=?").use {
                it.setString(1, json.encodeToString(request.controls))
                it.setString(2, cardId)
                check(it.executeUpdate() == 1)
            }
            OperationResult("CONTROLS_UPDATED", cardId)
        }
    }

    fun card(cardId: String): CardView = database.transaction { connection ->
        connection.prepareStatement(
            "SELECT id,company_id,holder,state,currency,limit_minor FROM cards WHERE id=?"
        ).use {
            it.setString(1, cardId)
            it.executeQuery().use { result ->
                require(result.next()) { "unknown card" }
                CardView(result.getString(1), result.getString(2), result.getString(3),
                    CardState.valueOf(result.getString(4)), result.getString(5).trim(),
                    result.getLong(6), available(connection, cardId))
            }
        }
    }

    internal fun available(connection: Connection, cardId: String): Long {
        val holds = connection.prepareStatement(
            "SELECT coalesce(sum(billing_minor-captured_billing_minor),0) " +
                "FROM authorizations WHERE card_id=? AND status='OPEN'"
        ).use {
            it.setString(1, cardId)
            it.executeQuery().use { result -> result.next(); result.getBigDecimal(1).longValueExact() }
        }
        return Math.subtractExact(Ledger.balance(connection, "card:$cardId:funding"), holds)
    }
}
