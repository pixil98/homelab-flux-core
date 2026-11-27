require ["fileinto", "mailbox"];

# rspamd's milter adds "X-Spam: Yes" for the add header action, and
# milter_headers strips any X-Spam header a sender put there themselves.
if header :is "X-Spam" "Yes" {
  fileinto :create "Spam";
}
