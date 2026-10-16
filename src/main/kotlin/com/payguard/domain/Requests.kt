package com.payguard.domain

import kotlinx.serialization.Serializable

@Serializable
data class IssueCardRequest(
    val companyId: String,
    val holder: String,
    val currency: String = "SGD",
    val limitMinor: Long,
    val idempotencyKey: String,
)

@Serializable
data class ChangeStateRequest(val state: CardState, val idempotencyKey: String)

@Serializable
data class SetControlsRequest(val controls: SpendControls, val idempotencyKey: String)

@Serializable
data class AuthorizeRequest(
    val authId: String,
    val cardId: String,
    val merchantId: String,
    val mcc: String,
    val country: String,
    val currency: String,
    val amountMinor: Long,
    val deviceId: String = "device_demo",
    val latitude: Double = 1.35,
    val longitude: Double = 103.82,
    val homeLatitude: Double = 1.35,
    val homeLongitude: Double = 103.82,
    val merchantRisk: Double = 0.1,
    val priorRefundRatio: Double = 0.0,
    val idempotencyKey: String,
)

@Serializable
data class CaptureRequest(val captureId: String, val authId: String, val amountMinor: Long, val idempotencyKey: String)

@Serializable
data class ReverseRequest(val authId: String, val idempotencyKey: String)

@Serializable
data class RefundRequest(val refundId: String, val captureId: String, val amountMinor: Long, val idempotencyKey: String)

@Serializable
data class OperationResult(
    val status: String,
    val reference: String,
    val amountMinor: Long? = null,
    val currency: String? = null,
    val reasons: List<String> = emptyList(),
    val modelScore: Double? = null,
    val modelAction: String? = null,
    val modelReasonCodes: List<String> = emptyList(),
    val modelVersion: String? = null,
)

@Serializable
data class AuthorizationEvent(val request: AuthorizeRequest, val result: OperationResult,
                              val timestamp: String)

@Serializable
data class CardView(val id: String, val companyId: String, val holder: String, val state: CardState,
                    val currency: String, val limitMinor: Long, val availableMinor: Long)
