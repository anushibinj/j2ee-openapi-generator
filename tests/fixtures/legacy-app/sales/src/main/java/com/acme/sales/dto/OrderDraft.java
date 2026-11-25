package com.acme.sales.dto;

import java.io.Serializable;
import java.util.ArrayList;
import java.util.List;
import com.acme.sales.model.OrderLine;

/** Session-scoped state of the order wizard. */
public class OrderDraft implements Serializable {

    private static final long serialVersionUID = 1L;

    private Long customerId;

    private String notes;

    private List<OrderLine> lines;

    public Long getCustomerId() {
        return customerId;
    }

    public void setCustomerId(Long customerId) {
        this.customerId = customerId;
    }

    public String getNotes() {
        return notes;
    }

    public void setNotes(String notes) {
        this.notes = notes;
    }

    public List<OrderLine> getLines() {
        return lines;
    }

    public void setLines(List<OrderLine> lines) {
        this.lines = lines;
    }
}
