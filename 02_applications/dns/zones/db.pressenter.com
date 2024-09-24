$ORIGIN pressenter.com.
$TTL    6h

@       SOA     ns1.reisman.org. hostmaster.pressenter.com. (
                2024060201      ; serial
                3h              ; refresh
                15m             ; retry
                4w              ; expire
                15m             ; negttl
                )

        NS      ns1.reisman.org.
        NS      ns2.reisman.org.

www     CNAME   madison.reisman.org.
;mail    CNAME   mail.reisman.org.

; Mail
; @       MX      10 mail.pressenter.com.
@       TXT     "v=spf1 -all"

_dmarc  TXT     "v=DMARC1; p=reject; sp=reject; fo=1;"
