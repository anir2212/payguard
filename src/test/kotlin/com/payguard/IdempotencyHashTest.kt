package com.payguard

import com.payguard.service.requestHash
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Test

class IdempotencyHashTest {
    @Test
    fun requestHashIsLowercaseHexSha256() {
        // FIPS 180-2 test vector; stored hashes from earlier versions use the same encoding.
        assertEquals("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad", requestHash("abc"))
        assertEquals(64, requestHash("{\"authId\":\"auth_1\"}").length)
    }
}
