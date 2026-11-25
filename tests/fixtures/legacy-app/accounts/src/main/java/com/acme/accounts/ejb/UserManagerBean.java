package com.acme.accounts.ejb;

import java.util.List;
import javax.annotation.security.RolesAllowed;
import javax.ejb.Stateless;
import javax.persistence.EntityManager;
import javax.persistence.OptimisticLockException;
import javax.persistence.PersistenceContext;

import com.acme.accounts.model.User;

@Stateless
@RolesAllowed({"admin", "user"})
public class UserManagerBean implements UserManagerLocal {

    @PersistenceContext(unitName = "accounts")
    private EntityManager em;

    @Override
    public List<User> findUsers(String nameFilter, int page, int size) {
        String like = nameFilter == null ? "%" : "%" + nameFilter.toLowerCase() + "%";
        return em.createQuery("select u from User u where lower(u.name) like :n order by u.name", User.class)
                .setParameter("n", like)
                .setFirstResult(page * size)
                .setMaxResults(size)
                .getResultList();
    }

    @Override
    public long countUsers(String nameFilter) {
        String like = nameFilter == null ? "%" : "%" + nameFilter.toLowerCase() + "%";
        return em.createQuery("select count(u) from User u where lower(u.name) like :n", Long.class)
                .setParameter("n", like)
                .getSingleResult();
    }

    @Override
    public User findUser(Long id) {
        return em.find(User.class, id);
    }

    @Override
    public User findByLogin(String login) {
        return em.createQuery("select u from User u where u.email = :l", User.class)
                .setParameter("l", login)
                .getSingleResult();
    }

    @Override
    @RolesAllowed("admin")
    public User create(User user) {
        em.persist(user);
        return user;
    }

    @Override
    public User update(User user, Long expectedVersion) {
        if (!expectedVersion.equals(user.getVersion())) {
            throw new OptimisticLockException("stale user " + user.getId());
        }
        return em.merge(user);
    }

    @Override
    @RolesAllowed("admin")
    public void delete(Long id) throws UserInUseException {
        long refs = em.createQuery("select count(p) from Phone p where p.user.id = :id", Long.class)
                .setParameter("id", id)
                .getSingleResult();
        if (refs > 0) {
            throw new UserInUseException(id);
        }
        em.remove(em.find(User.class, id));
    }
}
