package com.payguard

import com.payguard.db.Ledger
import com.payguard.domain.CardLifecycle
import com.payguard.domain.CardState
import com.payguard.domain.ControlContext
import com.payguard.domain.ControlEvaluator
import com.payguard.domain.FxRate
import com.payguard.domain.MoneyMinor
import com.payguard.domain.AuthorizeRequest
import com.payguard.domain.CaptureRequest
import com.payguard.domain.RefundRequest
import com.payguard.domain.SpendControls
import net.jqwik.api.Arbitraries
import net.jqwik.api.Arbitrary
import net.jqwik.api.ForAll
import net.jqwik.api.Property
import net.jqwik.api.Provide
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Test

class DomainPropertiesTest {
    @Provide
    fun positive(): Arbitrary<Long> = Arbitraries.longs().between(1L, 1_000_000_000L)

    @Property(tries = 200)
    fun fxHalfEvenIsIntegral(@ForAll("positive") minor: Long) {
        val converted = FxRate(135, 100).convert(MoneyMinor(minor, "USD"), "SGD")
        assertEquals("SGD", converted.currency)
        assert(converted.minor > 0L)
        assert(kotlin.math.abs(converted.minor * 100 - minor * 135) <= 50L)
    }

    @Property(tries = 250)
    fun randomJournalSequencesBalanceOnlyWithMatchingCredit(
        @ForAll("positive") debit: Long,
        @ForAll("positive") credit: Long,
    ) {
        val legs = listOf("customer" to -debit, "merchant" to credit)
        assertEquals(debit == credit, Ledger.isBalanced(legs))
        assert(Ledger.isBalanced(listOf("customer" to -debit, "merchant" to debit)))
    }

    @Test
    fun legalCardTransitionsAndTerminalStates() {
        assertEquals(CardState.ACTIVE, CardLifecycle.transition(CardState.CREATED, CardState.ACTIVE))
        assertEquals(CardState.FROZEN, CardLifecycle.transition(CardState.ACTIVE, CardState.FROZEN))
        assertEquals(CardState.ACTIVE, CardLifecycle.transition(CardState.FROZEN, CardState.ACTIVE))
        for (terminal in listOf(CardState.LOST, CardState.STOLEN, CardState.CANCELLED)) {
            assertThrows(IllegalArgumentException::class.java) { CardLifecycle.transition(terminal, CardState.ACTIVE) }
        }
    }

    @Test
    fun everySpendRuleCanDecline() {
        val context = ControlContext(100, "7995", "bad-shop", "US", 900, 900, 900, 900, 5)
        val policy = SpendControls(
            cardDailyMinor = 950, cardMonthlyMinor = 950, companyDailyMinor = 950,
            companyMonthlyMinor = 950, allowedMccs = setOf("5411"), deniedMccs = setOf("7995"),
            blockedMerchants = setOf("bad-shop"), allowedCountries = setOf("SG"), velocityCount = 5,
        )
        assertEquals(9, ControlEvaluator.declines(policy, context).size)
    }

    @Test
    fun amountFieldCannotBecomeDouble() {
        assertEquals(java.lang.Long.TYPE, MoneyMinor::class.java.getDeclaredField("minor").type)
        for (type in listOf(AuthorizeRequest::class.java, CaptureRequest::class.java,
            RefundRequest::class.java)) {
            assertEquals(java.lang.Long.TYPE, type.getDeclaredField("amountMinor").type)
        }
    }
}
