package com.acme.accounts.ejb;

import java.util.List;
import javax.ejb.Local;

import com.acme.accounts.model.User;

@Local
public interface UserManagerLocal {

    List<User> findUsers(String nameFilter, int page, int size);

    long countUsers(String nameFilter);

    User findUser(Long id);

    User findByLogin(String login);

    User create(User user);

    User update(User user, Long expectedVersion);

    void delete(Long id) throws UserInUseException;
}
