// Helpers for the user screens.
function loadUsers(page, cb) {
  var xhr = new XMLHttpRequest();
  xhr.open('GET', 'UserServlet?op=list&page=' + page);
  xhr.onload = function () { cb(xhr.responseText); };
  xhr.send();
}

function removeUser(id) {
  $.post('UserServlet', {op: 'delete', id: id}, function () {
    window.location.reload();
  });
}

function showUser(id) {
  $.get('UserServlet?op=read&id=' + id, function (html) {
    $('#detail').html(html);
  });
}
