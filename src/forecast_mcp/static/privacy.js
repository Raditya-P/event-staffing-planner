/* Fill in the contact address configured on the server (CONTACT_EMAIL), if any. */
fetch("/api/me").then((r) => r.json()).then((me) => {
  if (!me.contact) return;
  const span = document.getElementById("contact");
  const a = document.createElement("a");
  a.className = "link";
  a.href = "mailto:" + me.contact;
  a.textContent = me.contact;
  span.textContent = "";
  span.appendChild(a);
}).catch(() => {});
