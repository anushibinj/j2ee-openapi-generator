package com.acme.sales.web;

import java.io.IOException;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import javax.servlet.ServletException;
import javax.servlet.http.HttpServlet;
import javax.servlet.http.HttpServletRequest;
import javax.servlet.http.HttpServletResponse;

/** Reflection based dispatch: op=summary calls doSummary(request, response). */
public abstract class BaseServlet extends HttpServlet {

    private static final long serialVersionUID = 1L;

    @Override
    protected void doGet(HttpServletRequest request, HttpServletResponse response)
            throws ServletException, IOException {
        dispatch(request, response);
    }

    @Override
    protected void doPost(HttpServletRequest request, HttpServletResponse response)
            throws ServletException, IOException {
        dispatch(request, response);
    }

    private void dispatch(HttpServletRequest request, HttpServletResponse response)
            throws ServletException, IOException {
        String op = request.getParameter("op");
        if (op == null || op.isEmpty()) {
            response.sendError(HttpServletResponse.SC_BAD_REQUEST, "Missing op");
            return;
        }
        String methodName = "do" + capitalize(op);
        try {
            Method m = getClass().getMethod(methodName, HttpServletRequest.class, HttpServletResponse.class);
            m.invoke(this, request, response);
        } catch (NoSuchMethodException e) {
            response.sendError(HttpServletResponse.SC_NOT_FOUND, "Unknown op " + op);
        } catch (IllegalAccessException e) {
            throw new ServletException(e);
        } catch (InvocationTargetException e) {
            throw new ServletException(e.getCause());
        }
    }

    protected static String capitalize(String s) {
        return Character.toUpperCase(s.charAt(0)) + s.substring(1);
    }
}
