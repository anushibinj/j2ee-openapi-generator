<%@ page contentType="text/html;charset=UTF-8" language="java" %>
<%@ include file="/WEB-INF/jspf/header.jspf" %>
<html>
<body>
<h1>${user.name}</h1>
<dl>
  <dt><fmt:message key="user.email"/></dt><dd>${user.email}</dd>
  <dt><fmt:message key="user.status"/></dt><dd>${user.status}</dd>
  <dt><fmt:message key="user.creditLimit"/></dt><dd>${user.creditLimit}</dd>
</dl>
<ul>
  <c:forEach items="${user.phones}" var="p">
    <li>${p.label}: ${p.number}</li>
  </c:forEach>
</ul>
<a href="UserServlet?op=edit&id=${user.id}"><fmt:message key="action.edit"/></a>
<a href="UserServlet?op=list"><fmt:message key="action.back"/></a>

<form action="profile" method="post">
  <input type="hidden" name="op" value="update"/>
  <input type="text" name="displayName" maxlength="80" value="${user.name}"/>
  <input type="email" name="email" value="${user.email}"/>
  <select name="locale"><option value="en">en</option><option value="fr">fr</option></select>
  <button type="submit"><fmt:message key="action.save"/></button>
</form>
</body>
</html>
