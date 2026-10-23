package com.payguard.domain

import java.math.BigInteger

/** An ISO-4217 code shape, compiled once rather than per request. */
val CURRENCY_CODE = Regex("[A-Z]{3}")

/** Minor units only. A currency is explicit even when all local fixtures use SGD. */
data class MoneyMinor(val minor: Long, val currency: String) {
    init {
        require(currency.matches(CURRENCY_CODE)) { "ISO-4217 currency code required" }
    }

    fun requirePositive(): MoneyMinor {
        require(minor > 0) { "amount must be positive" }
        return this
    }
}

/** Exact rational fixed rate; round half to even in target minor units. */
data class FxRate(val numerator: Long, val denominator: Long) {
    init {
        require(numerator > 0 && denominator > 0)
    }

    fun convert(amount: MoneyMinor, target: String): MoneyMinor {
        amount.requirePositive()
        val product = BigInteger.valueOf(amount.minor).multiply(BigInteger.valueOf(numerator))
        val divisor = BigInteger.valueOf(denominator)
        val quotient = product.divide(divisor)
        val remainder = product.remainder(divisor)
        val doubled = remainder.multiply(BigInteger.TWO)
        val rounded = if (doubled > divisor || (doubled == divisor && quotient.testBit(0))) {
            quotient + BigInteger.ONE
        } else quotient
        return MoneyMinor(rounded.longValueExact(), target).requirePositive()
    }
}
