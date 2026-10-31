/* Own fixture composition: verifies the transport nonce is removed before FFI. */
#define ConfiguraIntSiTefInterativoEx FixtureConfigureWithoutEnvironmentCheck
#include "fixture.c"
#undef ConfiguraIntSiTefInterativoEx
#include <stdlib.h>
int ConfiguraIntSiTefInterativoEx(const char *server,const char *store,const char *terminal,short reserved,const char *additional) {
    if (getenv("SITEF_BRIDGE_TOKEN") != NULL) return 11;
    return FixtureConfigureWithoutEnvironmentCheck(server,store,terminal,reserved,additional);
}
