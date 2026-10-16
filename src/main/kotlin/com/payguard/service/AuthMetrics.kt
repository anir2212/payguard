package com.payguard.service

import com.payguard.domain.OperationResult
import java.util.concurrent.atomic.DoubleAdder
import java.util.concurrent.atomic.LongAdder

/** Bounded-label Prometheus text metrics for the local authorization path. */
object AuthMetrics {
    private val seconds = doubleArrayOf(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0)
    private val scoreBounds = doubleArrayOf(0.1, 0.2, 0.4, 0.6, 0.8, 1.0)
    private val durationBuckets = Array(seconds.size) { LongAdder() }
    private val scoreBuckets = Array(scoreBounds.size) { LongAdder() }
    private val durationSum = DoubleAdder()
    private val scoreSum = DoubleAdder()
    private val total = LongAdder()
    private val approved = LongAdder()
    private val declined = LongAdder()
    private val errors = LongAdder()
    private val modelDeclines = LongAdder()
    private val fallback = LongAdder()
    private val scored = LongAdder()

    fun record(result: OperationResult, elapsedSeconds: Double) {
        total.increment()
        if (result.status == "APPROVED") approved.increment() else declined.increment()
        if (result.modelAction == "MODEL_DECLINE") modelDeclines.increment()
        if (result.modelAction == "RULES_FALLBACK") fallback.increment()
        observeDuration(elapsedSeconds)
        result.modelScore?.let { score ->
            scored.increment()
            scoreSum.add(score)
            scoreBounds.forEachIndexed { index, bound ->
                if (score <= bound) scoreBuckets[index].increment()
            }
        }
    }

    fun recordError(elapsedSeconds: Double) {
        errors.increment()
        observeDuration(elapsedSeconds)
    }

    private fun observeDuration(value: Double) {
        durationSum.add(value)
        seconds.forEachIndexed { index, bound ->
            if (value <= bound) durationBuckets[index].increment()
        }
    }

    fun render(): String = buildString {
        appendLine("# HELP payguard_authorizations_total Completed authorization decisions")
        appendLine("# TYPE payguard_authorizations_total counter")
        appendLine("payguard_authorizations_total{status=\"APPROVED\"} ${approved.sum()}")
        appendLine("payguard_authorizations_total{status=\"DECLINED\"} ${declined.sum()}")
        appendLine("payguard_authorizations_total{status=\"ERROR\"} ${errors.sum()}")
        appendLine("# HELP payguard_model_decisions_total Model enforcement and rules fallback")
        appendLine("# TYPE payguard_model_decisions_total counter")
        appendLine("payguard_model_decisions_total{action=\"MODEL_DECLINE\"} ${modelDeclines.sum()}")
        appendLine("payguard_model_decisions_total{action=\"RULES_FALLBACK\"} ${fallback.sum()}")
        appendLine("# HELP payguard_auth_duration_seconds Local authorization service duration")
        appendLine("# TYPE payguard_auth_duration_seconds histogram")
        seconds.forEachIndexed { index, bound ->
            appendLine("payguard_auth_duration_seconds_bucket{le=\"$bound\"} ${durationBuckets[index].sum()}")
        }
        appendLine("payguard_auth_duration_seconds_bucket{le=\"+Inf\"} ${total.sum() + errors.sum()}")
        appendLine("payguard_auth_duration_seconds_sum ${durationSum.sum()}")
        appendLine("payguard_auth_duration_seconds_count ${total.sum() + errors.sum()}")
        appendLine("# HELP payguard_model_score Calibrated fraud score distribution")
        appendLine("# TYPE payguard_model_score histogram")
        scoreBounds.forEachIndexed { index, bound ->
            appendLine("payguard_model_score_bucket{le=\"$bound\"} ${scoreBuckets[index].sum()}")
        }
        appendLine("payguard_model_score_bucket{le=\"+Inf\"} ${scored.sum()}")
        appendLine("payguard_model_score_sum ${scoreSum.sum()}")
        appendLine("payguard_model_score_count ${scored.sum()}")
    }
}
