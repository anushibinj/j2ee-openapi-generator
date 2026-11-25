package com.acme.accounts.web;

import java.io.IOException;
import java.util.Date;
import java.util.List;
import javax.ejb.EJB;
import javax.persistence.OptimisticLockException;
import javax.servlet.ServletException;
import javax.servlet.http.HttpServlet;
import javax.servlet.http.HttpServletRequest;
import javax.servlet.http.HttpServletResponse;

import com.acme.accounts.ejb.UserInUseException;
import com.acme.accounts.ejb.UserManagerLocal;
import com.acme.accounts.model.User;
import com.acme.accounts.model.UserStatus;

/** User administration screens. Dispatches on the "op" request parameter. */
public class UserServlet extends HttpServlet {

    private static final long serialVersionUID = 1L;
    private static final int DEFAULT_PAGE_SIZE = 20;

    @EJB
    private UserManagerLocal userManager;

    @Override
    protected void doGet(HttpServletRequest request, HttpServletResponse response)
            throws ServletException, IOException {
        process(request, response);
    }

    @Override
    protected void doPost(HttpServletRequest request, HttpServletResponse response)
            throws ServletException, IOException {
        process(request, response);
    }

    private void process(HttpServletRequest request, HttpServletResponse response)
            throws ServletException, IOException {
        String op = request.getParameter("op");
        if ("list".equals(op)) {
            int page = parseInt(request.getParameter("page"), 0);
            int size = parseInt(request.getParameter("size"), DEFAULT_PAGE_SIZE);
            String name = request.getParameter("name");
            List<User> users = userManager.findUsers(name, page, size);
            request.setAttribute("users", users);
            request.setAttribute("total", userManager.countUsers(name));
            request.getRequestDispatcher("/userList.jsp").forward(request, response);
        } else if ("read".equalsIgnoreCase(op)) {
            Long id = Long.valueOf(request.getParameter("id"));
            User user = userManager.findUser(id);
            if (user == null) {
                response.sendError(HttpServletResponse.SC_NOT_FOUND, "User not found");
                return;
            }
            request.setAttribute("user", user);
            request.getRequestDispatcher("/userView.jsp").forward(request, response);
        } else if ("edit".equals(op)) {
            Long id = Long.valueOf(request.getParameter("id"));
            request.setAttribute("user", userManager.findUser(id));
            request.setAttribute("statuses", UserStatus.values());
            request.getRequestDispatcher("/userEdit.jsp").forward(request, response);
        } else if ("new".equals(op)) {
            request.setAttribute("user", new User());
            request.setAttribute("statuses", UserStatus.values());
            request.getRequestDispatcher("/userEdit.jsp").forward(request, response);
        } else if ("save".equals(op)) {
            saveUser(request, response);
        } else if ("delete".equals(op)) {
            Long id = Long.valueOf(request.getParameter("id"));
            try {
                userManager.delete(id);
                response.setStatus(HttpServletResponse.SC_NO_CONTENT);
            } catch (UserInUseException e) {
                response.sendError(409, "error.user.inuse");
            }
        } else {
            response.sendRedirect(request.getContextPath() + "/UserServlet?op=list");
        }
    }

    private void saveUser(HttpServletRequest request, HttpServletResponse response)
            throws ServletException, IOException {
        String idParam = request.getParameter("id");
        String name = request.getParameter("name");
        String email = request.getParameter("email");
        String status = request.getParameter("status");
        String birthDate = request.getParameter("birthDate");
        String version = request.getParameter("version");
        if (name == null || name.trim().isEmpty()) {
            request.setAttribute("error", "error.user.name.required");
            request.getRequestDispatcher("/userEdit.jsp").forward(request, response);
            return;
        }
        if (email == null || !email.matches("[^@]+@[^@]+")) {
            response.sendError(HttpServletResponse.SC_BAD_REQUEST, "Invalid email");
            return;
        }
        boolean creating = idParam == null || idParam.isEmpty();
        User user = creating ? new User() : userManager.findUser(Long.valueOf(idParam));
        user.setName(name.trim());
        user.setEmail(email);
        user.setStatus(status == null ? UserStatus.ACTIVE : UserStatus.valueOf(status));
        user.setBirthDate(birthDate == null ? null : new Date(java.sql.Date.valueOf(birthDate).getTime()));
        try {
            if (creating) {
                user = userManager.create(user);
            } else {
                user = userManager.update(user, Long.valueOf(version));
            }
        } catch (OptimisticLockException e) {
            response.sendError(HttpServletResponse.SC_CONFLICT, "Modified by another user");
            return;
        }
        response.sendRedirect(request.getContextPath() + "/UserServlet?op=read&id=" + user.getId());
    }

    private static int parseInt(String value, int fallback) {
        try {
            return Integer.parseInt(value);
        } catch (NumberFormatException e) {
            return fallback;
        }
    }
}
