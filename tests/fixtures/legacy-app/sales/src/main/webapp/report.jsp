<%@ page contentType="text/html;charset=UTF-8" language="java" %>
<%@ taglib prefix="c" uri="http://java.sun.com/jsp/jstl/core" %>
<%@ taglib prefix="fmt" uri="http://java.sun.com/jsp/jstl/fmt" %>
<fmt:setBundle basename="messages"/>
<html>
<body>
<form action="ReportServlet" method="get">
  <input type="hidden" name="op" value="summary"/>
  <input type="date" name="from" required="required"/>
  <input type="date" name="to" required="required"/>
  <button type="submit"><fmt:message key="action.search"/></button>
</form>
<table>
  <c:forEach items="${summary}" var="s">
    <tr><td>${s.orderNumber}</td><td>${s.customerName}</td><td>${s.total}</td></tr>
  </c:forEach>
</table>
<script>
  $.getJSON('ReportServlet?op=daily&day=2024-01-31', function (d) {
    $('#daily').text(d.orders + ' orders, ' + d.revenue);
  });
</script>
<span id="daily"></span>
</body>
</html>
