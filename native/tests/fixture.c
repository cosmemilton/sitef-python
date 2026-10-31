/* Original synthetic ABI fixture. This is not a CliSiTef implementation and
 * contains no vendor code, protocol traces, credentials, or payment behavior. */
#include <string.h>
#include <stdio.h>

static int step;
static int configured;
int ConfiguraIntSiTefInterativoEx(const char *server,const char *store,const char *terminal,short reserved,const char *additional) {
    if(strcmp(server,"127.0.0.1") || strcmp(store,"00000000") || strcmp(terminal,"AA123456") || reserved || strcmp(additional,"[X=1]")) return 11;
    configured=1; return 0;
}
int IniciaFuncaoSiTefInterativo(int function,const char *amount,const char *doc,const char *date,const char *time,const char *operator,const char *additional) {
    if(!configured) return -1;
    if(function==999) return -98765;
    if(strcmp(amount,"12,34") || strcmp(doc,"123") || strcmp(date,"20261030") || strcmp(time,"120000") || strcmp(operator,"test") || strcmp(additional,"{X=1}")) return -10;
    step=0;return 10000;
}
int ContinuaFuncaoSiTefInterativo(int *command,long *field,short *min,short *max,char *buffer,int capacity,int navigation) {
    if(capacity<20000) return -10;
    if(navigation==-1) return -2;
    if(step==0) {
        if(*command || *field || *min || *max || navigation || buffer[0]) return -12;
        *command=0;*field=121;*min=0;*max=0;
        /* Windows-1252 'Ação' and newline exercise real native encoding. */
        memcpy(buffer,"A\347\343o\n",6);step++;return 10000;
    }
    if(step==1) {
        if(*command || *field!=121 || *min || *max || memcmp(buffer,"A\347\343o\n",6)) return -12;
        *command=30;*field=999123;*min=1;*max=4;strcpy(buffer,"Collect");step++;return 10000;
    }
    if(*command!=30 || *field!=999123 || *min!=1 || *max!=4 || strcmp(buffer,"1234")) return -12;
    *command=0;*field=5005;*min=0;*max=0;buffer[0]=0;return 0;
}
void FinalizaFuncaoSiTefInterativo(short confirm,const char *doc,const char *date,const char *time,const char *additional) {
    (void)confirm;(void)doc;(void)date;(void)time;(void)additional;
}
int ObtemVersao(char *library,char *interface) {strcpy(library,"fixture-1");strcpy(interface,"fixture-interface-1");return 0;}
int ObtemQuantidadeTransacoesPendentes(const char *date,const char *doc) {(void)date;(void)doc;return 2;}
int DescarregaMensagens(void) {return -999;}
int VerificaPresencaPinPad(void) {return 1;}
int ValidaCampoCodigoEmBarras(const char *data,short *kind) {(void)data;*kind=1;return 3;}
int LeSenhaDireto(const char *key,char *encrypted) {if(strlen(key)!=64)return -10;strcpy(encrypted,"synthetic-encrypted");return 0;}

