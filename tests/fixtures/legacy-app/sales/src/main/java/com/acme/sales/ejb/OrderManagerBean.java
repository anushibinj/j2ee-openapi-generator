package com.acme.sales.ejb;

import java.math.BigDecimal;
import java.util.ArrayList;
import java.util.Date;
import java.util.List;
import javax.annotation.security.RolesAllowed;
import javax.ejb.Stateless;
import javax.persistence.EntityManager;
import javax.persistence.PersistenceContext;

import com.acme.sales.dto.OrderDraft;
import com.acme.sales.dto.OrderSummaryDto;
import com.acme.sales.model.Customer;
import com.acme.sales.model.Order;
import com.acme.sales.model.OrderLine;
import com.acme.sales.model.OrderStatus;

@Stateless
@RolesAllowed({"sales", "manager"})
public class OrderManagerBean implements OrderManagerLocal {

    @PersistenceContext(unitName = "sales")
    private EntityManager em;

    @Override
    public List<Order> findOrders(String status, Long customerId, int page, int size) {
        return em.createQuery("select o from Order o order by o.orderDate desc", Order.class)
                .setFirstResult(page * size)
                .setMaxResults(size)
                .getResultList();
    }

    @Override
    public long countOrders(String status, Long customerId) {
        return em.createQuery("select count(o) from Order o", Long.class).getSingleResult();
    }

    @Override
    public Order findById(Long id) {
        return em.find(Order.class, id);
    }

    @Override
    public OrderSummaryDto findSummary(String orderNumber) {
        Order o = em.createQuery("select o from Order o where o.orderNumber = :n", Order.class)
                .setParameter("n", orderNumber)
                .getSingleResult();
        return toDto(o);
    }

    @Override
    @RolesAllowed("sales")
    public Order createFromDraft(OrderDraft draft) {
        Order order = new Order();
        order.setCustomer(em.find(Customer.class, draft.getCustomerId()));
        order.setNotes(draft.getNotes());
        order.setStatus(OrderStatus.SUBMITTED);
        order.setOrderDate(new Date());
        BigDecimal total = BigDecimal.ZERO;
        for (OrderLine line : draft.getLines()) {
            line.setOrder(order);
            total = total.add(line.getUnitPrice().multiply(BigDecimal.valueOf(line.getQuantity())));
        }
        order.setLines(draft.getLines());
        order.setTotal(total);
        em.persist(order);
        return order;
    }

    @Override
    public List<OrderSummaryDto> summarize(String from, String to) {
        List<OrderSummaryDto> result = new ArrayList<OrderSummaryDto>();
        for (Order o : findOrders(null, null, 0, 1000)) {
            result.add(toDto(o));
        }
        return result;
    }

    @Override
    public OrderSummaryDto dailyTotals(String day) {
        OrderSummaryDto dto = new OrderSummaryDto();
        dto.setOrderNumber(day);
        dto.setTotal(BigDecimal.ZERO);
        return dto;
    }

    private OrderSummaryDto toDto(Order o) {
        OrderSummaryDto dto = new OrderSummaryDto();
        dto.setOrderNumber(o.getOrderNumber());
        dto.setCustomerName(o.getCustomer().getName());
        dto.setStatus(o.getStatus().name());
        dto.setTotal(o.getTotal());
        dto.setLineCount(o.getLines().size());
        return dto;
    }
}
