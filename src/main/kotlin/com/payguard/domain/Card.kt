package com.payguard.domain

import kotlinx.serialization.Serializable

@Serializable
enum class CardState { CREATED, ACTIVE, FROZEN, LOST, STOLEN, CANCELLED }

object CardLifecycle {
    private val allowed = mapOf(
        CardState.CREATED to setOf(CardState.ACTIVE, CardState.CANCELLED),
        CardState.ACTIVE to setOf(CardState.FROZEN, CardState.LOST, CardState.STOLEN, CardState.CANCELLED),
        CardState.FROZEN to setOf(CardState.ACTIVE, CardState.LOST, CardState.STOLEN, CardState.CANCELLED),
        CardState.LOST to setOf(CardState.CANCELLED),
        CardState.STOLEN to setOf(CardState.CANCELLED),
        CardState.CANCELLED to emptySet(),
    )

    fun transition(from: CardState, to: CardState): CardState {
        require(to in allowed.getValue(from)) { "illegal card transition: $from -> $to" }
        return to
    }
}

@Serializable
data class SpendControls(
    val cardDailyMinor: Long? = null,
    val cardMonthlyMinor: Long? = null,
    val companyDailyMinor: Long? = null,
    val companyMonthlyMinor: Long? = null,
    val allowedMccs: Set<String> = emptySet(),
    val deniedMccs: Set<String> = emptySet(),
    val blockedMerchants: Set<String> = emptySet(),
    val allowedCountries: Set<String> = emptySet(),
    val velocityCount: Int = 5,
    val velocitySeconds: Int = 60,
) {
    init {
        require(listOfNotNull(cardDailyMinor, cardMonthlyMinor, companyDailyMinor, companyMonthlyMinor).all { it > 0 })
        require(velocityCount > 0 && velocitySeconds > 0)
    }
}

data class ControlContext(
    val amountMinor: Long,
    val mcc: String,
    val merchantId: String,
    val country: String,
    val cardDaySpend: Long,
    val cardMonthSpend: Long,
    val companyDaySpend: Long,
    val companyMonthSpend: Long,
    val recentAttempts: Int,
)

object ControlEvaluator {
    fun declines(policy: SpendControls, context: ControlContext): List<String> = buildList {
        if (policy.allowedMccs.isNotEmpty() && context.mcc !in policy.allowedMccs) add("mcc_not_allowed")
        if (context.mcc in policy.deniedMccs) add("mcc_denied")
        if (context.merchantId in policy.blockedMerchants) add("merchant_blocked")
        if (policy.allowedCountries.isNotEmpty() && context.country !in policy.allowedCountries) add("country_not_allowed")
        if (policy.cardDailyMinor != null && context.amountMinor > policy.cardDailyMinor - context.cardDaySpend) add("card_daily_limit")
        if (policy.cardMonthlyMinor != null && context.amountMinor > policy.cardMonthlyMinor - context.cardMonthSpend) add("card_monthly_limit")
        if (policy.companyDailyMinor != null && context.amountMinor > policy.companyDailyMinor - context.companyDaySpend) add("company_daily_limit")
        if (policy.companyMonthlyMinor != null && context.amountMinor > policy.companyMonthlyMinor - context.companyMonthSpend) add("company_monthly_limit")
        if (context.recentAttempts >= policy.velocityCount) add("velocity")
    }
}
