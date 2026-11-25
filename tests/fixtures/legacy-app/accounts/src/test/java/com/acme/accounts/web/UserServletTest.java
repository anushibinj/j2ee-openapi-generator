package com.acme.accounts.web;

import org.junit.Test;

/** Lives under a test directory so the default exclude globs must skip it. */
public class UserServletTest {

    @Test
    public void listOpIsDispatched() {
        String op = "list";
        if ("list".equals(op)) {
            return;
        }
        throw new AssertionError("op=hidden must never be indexed");
    }
}
