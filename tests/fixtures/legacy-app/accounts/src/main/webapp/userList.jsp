<%@ page contentType="text/html;charset=UTF-8" language="java" %>
<%@ include file="/WEB-INF/jspf/header.jspf" %>
<%-- Single line comment: <a href="UserServlet?op=ghost">never rendered</a> --%>
<%!
    private String badge(Object status) {
        return "ACTIVE".equals(String.valueOf(status)) ? "ok" : "warn";
    }
%>
<html>
<head>
  <title><fmt:message key="user.list.title"/></title>
  <script src="js/users.js"></script>
</head>
<body>
<jsp:include page="/WEB-INF/jspf/header.jspf"/>

<%--
  Multi-line comment, the filter form below posts nothing:
  <form action="UserServlet" method="post">
    <input type="hidden" name="op" value="purge"/>
  </form>
--%>
<form action="UserServlet" method="get">
  <input type="hidden" name="op" value="list"/>
  <label><fmt:message key="user.name"/>
    <input type="text" name="name" maxlength="80" value="${param.name}"/>
  </label>
  <select name="size">
    <option value="10">10</option>
    <option value="20" selected="selected">20</option>
    <option value="50">50</option>
  </select>
  <input type="hidden" name="page" value="0"/>
  <button type="submit"><fmt:message key="action.search"/></button>
</form>

<table>
  <tr><th><fmt:message key="user.name"/></th><th><fmt:message key="user.email"/></th><th></th></tr>
  <c:forEach items="${users}" var="u">
    <tr class="<%= badge(pageContext.findAttribute("u")) %>">
      <td><a href="UserServlet?op=read&id=${u.id}">${u.name}</a></td>
      <td>${u.email}</td>
      <td>
        <a href="UserServlet?op=edit&id=${u.id}"><fmt:message key="action.edit"/></a>
        <button class="del" data-id="${u.id}"><fmt:message key="action.delete"/></button>
      </td>
    </tr>
  </c:forEach>
</table>
<a href="UserServlet?op=new"><fmt:message key="user.new"/></a>
<% if (request.getAttribute("total") != null) { %>
  <p>Total: <%= request.getAttribute("total") %></p>
<% } %>

<script>
  $('.del').click(function () {
    var id = $(this).data('id');
    $.ajax({
      url: 'UserServlet?op=delete',
      type: 'POST',
      data: {id: id},
      success: function () { location.reload(); },
      error: function (xhr) {
        if (xhr.status === 409) { alert('<fmt:message key="error.user.inuse"/>'); }
      }
    });
  });
</script>
</body>
</html>
