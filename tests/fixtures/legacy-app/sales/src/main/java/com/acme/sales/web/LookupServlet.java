package com.acme.sales.web;

import java.io.IOException;
import java.util.HashMap;
import java.util.Map;
import javax.servlet.ServletException;
import javax.servlet.http.HttpServlet;
import javax.servlet.http.HttpServletRequest;
import javax.servlet.http.HttpServletResponse;

/** Small reference data lookups, dispatched through a map of handlers. */
public class LookupServlet extends HttpServlet {

    private static final long serialVersionUID = 1L;

    interface Handler {
        void handle(HttpServletRequest request, HttpServletResponse response) throws IOException;
    }

    private final Map<String, Handler> handlers = new HashMap<String, Handler>();

    @Override
    public void init() throws ServletException {
        handlers.put("countries", this::writeCountries);
        handlers.put("statuses", this::writeStatuses);
    }

    @Override
    protected void doGet(HttpServletRequest request, HttpServletResponse response)
            throws ServletException, IOException {
        Handler handler = handlers.get(request.getParameter("op"));
        if (handler == null) {
            response.sendError(HttpServletResponse.SC_NOT_FOUND);
            return;
        }
        handler.handle(request, response);
    }

    private void writeCountries(HttpServletRequest request, HttpServletResponse response) throws IOException {
        response.setContentType("application/json");
        response.getWriter().write("[{\"code\":\"US\",\"name\":\"United States\"},"
                + "{\"code\":\"FR\",\"name\":\"France\"}]");
    }

    private void writeStatuses(HttpServletRequest request, HttpServletResponse response) throws IOException {
        response.setContentType("application/json");
        response.getWriter().write("[\"DRAFT\",\"SUBMITTED\",\"SHIPPED\",\"CANCELLED\"]");
    }
}
