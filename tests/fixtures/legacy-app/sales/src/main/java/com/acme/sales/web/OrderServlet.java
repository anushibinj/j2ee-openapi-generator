package com.acme.sales.web;

import java.io.IOException;
import java.io.PrintWriter;
import java.math.BigDecimal;
import java.util.ArrayList;
import javax.ejb.EJB;
import javax.servlet.ServletException;
import javax.servlet.http.HttpServlet;
import javax.servlet.http.HttpServletRequest;
import javax.servlet.http.HttpServletResponse;
import javax.servlet.http.HttpSession;

import com.acme.sales.dto.OrderDraft;
import com.acme.sales.ejb.OrderManagerLocal;
import com.acme.sales.model.Order;
import com.acme.sales.model.OrderLine;

/** Order screens and the three step order wizard. */
public class OrderServlet extends HttpServlet {

    private static final long serialVersionUID = 1L;

    static final String OP_LIST = "list";
    static final String OP_VIEW = "view";
    static final String OP_DETAIL = "detail";
    static final String OP_STEP1 = "step1";
    static final String OP_STEP2 = "step2";
    static final String OP_CONFIRM = "confirm";
    static final String OP_EXPORT = "export";

    @EJB
    private OrderManagerLocal orderManager;

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
        if (op == null) {
            op = "";
        }
        HttpSession session = request.getSession();
        switch (op) {
        case OP_LIST: {
            int page = Integer.parseInt(request.getParameter("page") == null ? "0" : request.getParameter("page"));
            int size = Integer.parseInt(request.getParameter("size") == null ? "20" : request.getParameter("size"));
            String status = request.getParameter("status");
            String customerId = request.getParameter("customerId");
            Long cid = customerId == null ? null : Long.valueOf(customerId);
            request.setAttribute("orders", orderManager.findOrders(status, cid, page, size));
            request.setAttribute("total", orderManager.countOrders(status, cid));
            request.getRequestDispatcher("/orderList.jsp").forward(request, response);
            break;
        }
        case OP_VIEW: {
            Order order = orderManager.findById(Long.valueOf(request.getParameter("id")));
            if (order == null) {
                response.sendError(HttpServletResponse.SC_NOT_FOUND);
                return;
            }
            request.setAttribute("order", order);
            request.getRequestDispatcher("/orderList.jsp").forward(request, response);
            break;
        }
        case OP_DETAIL: {
            String orderNumber = request.getParameter("orderNumber");
            request.setAttribute("summary", orderManager.findSummary(orderNumber));
            request.getRequestDispatcher("/orderList.jsp").forward(request, response);
            break;
        }
        case OP_STEP1: {
            if ("POST".equals(request.getMethod())) {
                OrderDraft draft = new OrderDraft();
                draft.setCustomerId(Long.valueOf(request.getParameter("customerId")));
                draft.setNotes(request.getParameter("notes"));
                draft.setLines(new ArrayList<OrderLine>());
                session.setAttribute("orderDraft", draft);
                request.getRequestDispatcher("/wizardStep2.jsp").forward(request, response);
            } else {
                request.getRequestDispatcher("/wizardStep1.jsp").forward(request, response);
            }
            break;
        }
        case OP_STEP2: {
            OrderDraft draft = (OrderDraft) session.getAttribute("orderDraft");
            if (draft == null) {
                response.sendError(HttpServletResponse.SC_BAD_REQUEST, "No order in progress");
                return;
            }
            OrderLine line = new OrderLine();
            line.setSku(request.getParameter("sku"));
            line.setQuantity(Integer.parseInt(request.getParameter("quantity")));
            line.setUnitPrice(new BigDecimal(request.getParameter("unitPrice")));
            draft.getLines().add(line);
            session.setAttribute("orderDraft", draft);
            request.getRequestDispatcher("/wizardStep2.jsp").forward(request, response);
            break;
        }
        case OP_CONFIRM: {
            OrderDraft draft = (OrderDraft) session.getAttribute("orderDraft");
            if (draft == null || draft.getLines().isEmpty()) {
                response.sendError(HttpServletResponse.SC_BAD_REQUEST, "Nothing to confirm");
                return;
            }
            Order order = orderManager.createFromDraft(draft);
            session.removeAttribute("orderDraft");
            response.sendRedirect(request.getContextPath() + "/OrderServlet?op=view&id=" + order.getId());
            break;
        }
        case OP_EXPORT: {
            String status = request.getParameter("status");
            response.setContentType("text/csv");
            response.setHeader("Content-Disposition", "attachment; filename=\"orders.csv\"");
            PrintWriter out = response.getWriter();
            out.println("orderNumber,customer,status,total");
            for (Order o : orderManager.findOrders(status, null, 0, Integer.MAX_VALUE)) {
                out.println(o.getOrderNumber() + "," + o.getCustomer().getName() + "," + o.getStatus() + "," + o.getTotal());
            }
            out.flush();
            break;
        }
        default:
            response.sendRedirect(request.getContextPath() + "/OrderServlet?op=list");
        }
    }
}
