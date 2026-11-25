// Helpers for the order screens.
function loadStatuses(cb) {
  $.get('LookupServlet?op=statuses', cb);
}

function downloadCsv(status) {
  var xhr = new XMLHttpRequest();
  xhr.open('GET', 'OrderServlet?op=export&status=' + encodeURIComponent(status));
  xhr.responseType = 'blob';
  xhr.send();
}
