export {};
const form = document.getElementById("login") as HTMLFormElement;
const status = document.getElementById("status")!;
void chrome.runtime.sendMessage({ type: "state" }).then((result: { ok: boolean; value?: { user: { email: string } | null } }) => {
  const email = result.ok ? result.value?.user?.email : undefined;
  if (!email) return;
  document.getElementById("current-email")!.textContent = email;
  document.getElementById("current")!.classList.remove("hidden");
});
form.addEventListener("submit", (event) => {
  event.preventDefault();
  status.textContent = "Signing in…";
  const button = form.querySelector("button")!;
  button.disabled = true;
  void chrome.runtime.sendMessage({
    type: "login",
    email: (document.getElementById("email") as HTMLInputElement).value.trim().toLowerCase(),
    password: (document.getElementById("password") as HTMLInputElement).value,
  }).then((result: { ok: boolean; error?: string }) => {
    if (!result.ok) throw new Error(result.error ?? "Login failed");
    (document.getElementById("password") as HTMLInputElement).value = "";
    form.classList.add("hidden");
    status.textContent = "Signed in. Return to Higgsfield and select your shot to start working.";
  }).catch((error: Error) => { status.textContent = error.message; })
    .finally(() => { button.disabled = false; });
});
