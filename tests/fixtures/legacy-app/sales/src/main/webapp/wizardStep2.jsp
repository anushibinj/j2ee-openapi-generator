<%@ page contentType="text/html;charset=UTF-8" language="java" %>
<%@ taglib prefix="c" uri="http://java.sun.com/jsp/jstl/core" %>
<%@ taglib prefix="fmt" uri="http://java.sun.com/jsp/jstl/fmt" %>
<fmt:setBundle basename="messages"/>
<%
    // scriptlet: nothing to do except read the wizard state
    Object draft = session.getAttribute("orderDraft");
%>
<html>
<body>
<%-- Step 2 of 3: add lines --%>
<form action="OrderServlet" method="post">
  <input type="hidden" name="op" value="step2"/>
  <input type="text" name="sku" required="required" pattern="[A-Z0-9-]{3,20}" maxlength="20"/>
  <input type="number" name="quantity" min="1" required="required"/>
  <input type="text" name="unitPrice" required="required" pattern="[0-9]+(\.[0-9]{2})?"/>
  <button type="submit"><fmt:message key="action.add"/></button>
</form>

<c:forEach items="${sessionScope.orderDraft.lines}" var="l">
  <div>${l.sku} x ${l.quantity}</div>
</c:forEach>

<%-- Step 3 of 3: confirm --%>
<form action="OrderServlet" method="post">
  <input type="hidden" name="op" value="confirm"/>
  <button type="submit"><fmt:message key="action.confirm"/></button>
</form>
</body>
</html>
