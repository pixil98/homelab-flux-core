$ORIGIN pressenter.com.
$TTL    6h

@       SOA     ns1.reisman.org. hostmaster.pressenter.com. (
                2023122501      ; serial
                3h              ; refresh
                15m             ; retry
                4w              ; expire
                15m             ; negttl
                )

        NS      ns1.reisman.org.
        NS      ns2.reisman.org.

@       A       216.165.140.134

www     A       216.165.140.134

; mail    A       216.165.140.134

; Mail
; @       MX      10 mail.pressenter.com.
;         TXT     "v=spf1 a:mail.pressenter.com -all"
@       TXT     "v=spf1 -all"

20220424._domainkey     TXT     ( "v=DKIM1; h=sha256; k=rsa; p="
                        "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAt119qMkb1zWet7N1LU6u"
                        "VG9N8pK6eovIJlO/hXscVgrL2gz99oCoy9nwh3RHXeJhRUE1TjPP7few3ccFhhmR"
                        "rjfD23kK0B+9JzYwmxIa9hXO1VMsqDRgJ3vZ6dbs0AHvHLN3tQm4ZHe/bivw6oXx"
                        "par97t6s/GbBIp2uXf1g6wSFTvgjWOXgWuPlKf47ZPgkeZa6IWAPVFZEiSGOmE5b"
                        "cWJiIcds3tJsELWcmUMxL6FTpijb6Fs6WK3NaNVDfZP26Es40AVk/g+IVzyW5g0e"
                        "CsMVHVJd+gxOv3OtR2BlJ+DsT2P3JTvGt5H7eqlNaG6CgTtR8nO7KlAaih1FYOku"
                        "LwIDAQAB" )  ; ----- DKIM key 20220424 for pressenter.com

; _dmarc  TXT     "v=DMARC1; p=reject; rua=mailto:dmarc-rua@pressenter.com; ruf=mailto:dmarc-ruf@pressenter.com; sp=reject; fo=1;"
_dmarc  TXT     "v=DMARC1; p=reject; sp=reject; fo=1;"
