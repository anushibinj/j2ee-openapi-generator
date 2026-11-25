<%@ page contentType="text/html;charset=UTF-8" language="java" %>
<%@ include file="/WEB-INF/jspf/header.jspf" %>
<html>
<body>
<c:if test="${not empty error}">
  <div class="error"><fmt:message key="${error}"/></div>
</c:if>

<form action="UserServlet" method="post">
  <input type="hidden" name="op" value="save"/>
  <input type="hidden" name="id" value="${user.id}"/>
  <input type="hidden" name="version" value="${user.version}"/>

  <label><fmt:message key="user.name"/>
    <input type="text" name="name" value="${user.name}" required="required" maxlength="80"/>
  </label>
  <label><fmt:message key="user.email"/>
    <input type="email" name="email" value="${user.email}" required="required" pattern="[^@]+@[^@]+"/>
  </label>
  <label><fmt:message key="user.status"/>
    <select name="status">
      <c:forEach items="${statuses}" var="s">
        <option value="${s}" ${s == user.status ? 'selected' : ''}>${s}</option>
      </c:forEach>
    </select>
  </label>
  <label><fmt:message key="user.birthDate"/>
    <input type="date" name="birthDate" value="${user.birthDate}"/>
  </label>
  <button type="submit"><fmt:message key="action.save"/></button>
</form>
</body>
</html>
