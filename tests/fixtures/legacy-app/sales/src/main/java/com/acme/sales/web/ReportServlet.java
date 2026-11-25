package com.acme.sales.web;

import java.io.IOException;
import java.io.PrintWriter;
import javax.ejb.EJB;
import javax.servlet.ServletException;
import javax.servlet.http.HttpServletRequest;
import javax.servlet.http.HttpServletResponse;

import com.acme.sales.dto.OrderSummaryDto;
import com.acme.sales.ejb.OrderManagerLocal;

/** Reports. Operations are the do* methods found by BaseServlet. */
public class ReportServlet extends BaseServlet {

    private static final long serialVersionUID = 1L;

    @EJB
    private OrderManagerLocal orderManager;

    public void doSummary(HttpServletRequest request, HttpServletResponse response)
            throws ServletException, IOException {
        String from = request.getParameter("from");
        String to = request.getParameter("to");
        request.setAttribute("summary", orderManager.summarize(from, to));
        request.getRequestDispatcher("/report.jsp").forward(request, response);
    }

    public void doDaily(HttpServletRequest request, HttpServletResponse response)
            throws ServletException, IOException {
        String day = request.getParameter("day");
        OrderSummaryDto totals = orderManager.dailyTotals(day);
        response.setContentType("application/json");
        PrintWriter out = response.getWriter();
        out.print("{\"day\":\"" + day + "\",\"orders\":" + totals.getLineCount()
                + ",\"revenue\":\"" + totals.getTotal() + "\"}");
    }
}
