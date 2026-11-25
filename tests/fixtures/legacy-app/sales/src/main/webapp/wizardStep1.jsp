<%@ page contentType="text/html;charset=UTF-8" language="java" %>
<%@ taglib prefix="fmt" uri="http://java.sun.com/jsp/jstl/fmt" %>
<fmt:setBundle basename="messages"/>
<html>
<body>
<%-- Step 1 of 3: choose customer --%>
<form action="OrderServlet" method="post">
  <input type="hidden" name="op" value="step1"/>
  <label><fmt:message key="order.customer"/>
    <input type="number" name="customerId" required="required" min="1"/>
  </label>
  <label><fmt:message key="order.notes"/>
    <textarea name="notes" maxlength="500"></textarea>
  </label>
  <select id="country" name="country"></select>
  <button type="submit"><fmt:message key="action.next"/></button>
</form>
<script>
  fetch('LookupServlet?op=countries')
    .then(function (r) { return r.json(); })
    .then(function (list) {
      list.forEach(function (c) {
        document.getElementById('country').add(new Option(c.name, c.code));
      });
    });
</script>
</body>
</html>
