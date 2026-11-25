package com.acme.sales.ejb;

import java.util.List;
import javax.ejb.Local;

import com.acme.sales.dto.OrderDraft;
import com.acme.sales.dto.OrderSummaryDto;
import com.acme.sales.model.Order;

@Local
public interface OrderManagerLocal {

    List<Order> findOrders(String status, Long customerId, int page, int size);

    long countOrders(String status, Long customerId);

    Order findById(Long id);

    OrderSummaryDto findSummary(String orderNumber);

    Order createFromDraft(OrderDraft draft);

    List<OrderSummaryDto> summarize(String from, String to);

    OrderSummaryDto dailyTotals(String day);
}
