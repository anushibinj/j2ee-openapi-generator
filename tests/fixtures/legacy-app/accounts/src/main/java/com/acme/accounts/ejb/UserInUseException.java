package com.acme.accounts.ejb;

import javax.ejb.ApplicationException;

/** Thrown when a user cannot be deleted because other records still reference it. */
@ApplicationException(rollback = true)
public class UserInUseException extends Exception {

    private static final long serialVersionUID = 1L;

    private final Long userId;

    public UserInUseException(Long userId) {
        super("User " + userId + " is still referenced");
        this.userId = userId;
    }

    public Long getUserId() {
        return userId;
    }
}
