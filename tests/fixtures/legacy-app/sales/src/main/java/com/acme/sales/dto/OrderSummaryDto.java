package com.acme.sales.dto;

import java.io.Serializable;
import java.math.BigDecimal;

/** Read-only projection used by the reports and the detail view. */
public class OrderSummaryDto implements Serializable {

    private static final long serialVersionUID = 1L;

    private String orderNumber;

    private String customerName;

    private String status;

    private BigDecimal total;

    private int lineCount;

    public String getOrderNumber() {
        return orderNumber;
    }

    public void setOrderNumber(String orderNumber) {
        this.orderNumber = orderNumber;
    }

    public String getCustomerName() {
        return customerName;
    }

    public void setCustomerName(String customerName) {
        this.customerName = customerName;
    }

    public String getStatus() {
        return status;
    }

    public void setStatus(String status) {
        this.status = status;
    }

    public BigDecimal getTotal() {
        return total;
    }

    public void setTotal(BigDecimal total) {
        this.total = total;
    }

    public int getLineCount() {
        return lineCount;
    }

    public void setLineCount(int lineCount) {
        this.lineCount = lineCount;
    }
}
