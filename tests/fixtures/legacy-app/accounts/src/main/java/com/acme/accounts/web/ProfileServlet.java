package com.acme.accounts.web;

import java.io.IOException;
import javax.ejb.EJB;
import javax.servlet.ServletException;
import javax.servlet.annotation.WebServlet;
import javax.servlet.http.HttpServlet;
import javax.servlet.http.HttpServletRequest;
import javax.servlet.http.HttpServletResponse;

import com.acme.accounts.ejb.UserManagerLocal;
import com.acme.accounts.model.User;

/** The signed-in user's own profile. */
@WebServlet(name = "ProfileServlet", urlPatterns = {"/profile"})
public class ProfileServlet extends HttpServlet {

    private static final long serialVersionUID = 1L;
    private static final String OP_VIEW = "view";
    private static final String OP_UPDATE = "update";

    @EJB
    private UserManagerLocal userManager;

    @Override
    protected void doGet(HttpServletRequest request, HttpServletResponse response)
            throws ServletException, IOException {
        handle(request, response);
    }

    @Override
    protected void doPost(HttpServletRequest request, HttpServletResponse response)
            throws ServletException, IOException {
        handle(request, response);
    }

    private void handle(HttpServletRequest request, HttpServletResponse response)
            throws ServletException, IOException {
        String op = request.getParameter("op");
        User me = userManager.findByLogin(request.getRemoteUser());
        if (OP_VIEW.equals(op)) {
            request.setAttribute("user", me);
            request.getRequestDispatcher("/userView.jsp").forward(request, response);
        } else if (OP_UPDATE.equals(op)) {
            String displayName = request.getParameter("displayName");
            String email = request.getParameter("email");
            String locale = request.getParameter("locale");
            if (displayName == null || displayName.length() > 80) {
                response.sendError(HttpServletResponse.SC_BAD_REQUEST, "Invalid display name");
                return;
            }
            me.setName(displayName);
            me.setEmail(email);
            me.setLocale(locale);
            userManager.update(me, me.getVersion());
            response.sendRedirect(request.getContextPath() + "/profile?op=view");
        }
    }
}
