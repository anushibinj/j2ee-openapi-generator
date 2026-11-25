<%@ page contentType="text/html;charset=UTF-8" language="java" %>
<%@ taglib prefix="c" uri="http://java.sun.com/jsp/jstl/core" %>
<%@ taglib prefix="fmt" uri="http://java.sun.com/jsp/jstl/fmt" %>
<fmt:setBundle basename="messages"/>
<%-- Order list, also reused for the view and detail ops --%>
<html>
<head><script src="js/orders.js"></script></head>
<body>
<form action="OrderServlet" method="get">
  <input type="hidden" name="op" value="list"/>
  <select name="status">
    <option value=""></option>
    <option value="DRAFT">Draft</option>
    <option value="SUBMITTED">Submitted</option>
    <option value="SHIPPED">Shipped</option>
  </select>
  <input type="number" name="customerId" min="1"/>
  <input type="hidden" name="page" value="0"/>
  <input type="hidden" name="size" value="20"/>
  <button type="submit"><fmt:message key="action.search"/></button>
</form>

<table>
  <c:forEach items="${orders}" var="o">
    <tr>
      <td><a href="OrderServlet?op=view&id=${o.id}">${o.orderNumber}</a></td>
      <td>${o.customer.name}</td>
      <td>${o.status}</td>
      <td><fmt:formatNumber value="${o.total}" type="currency"/></td>
      <td><a href="OrderServlet?op=detail&orderNumber=${o.orderNumber}">detail</a></td>
    </tr>
  </c:forEach>
</table>
<a href="OrderServlet?op=step1"><fmt:message key="order.new"/></a>
<a href="OrderServlet?op=export&status=SUBMITTED"><fmt:message key="order.export"/></a>
</body>
</html>
