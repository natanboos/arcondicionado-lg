# Ar-condicionado LG ThinQ (Windows)

Painel flutuante para controlar o ar-condicionado LG pela **API oficial LG ThinQ Connect**
(SDK [`thinqconnect`](https://pypi.org/project/thinqconnect/)).

> A primeira versão usava a API interna do app do celular (biblioteca `wideq` do
> [ha-smartthinq-sensors](https://github.com/ollo69/ha-smartthinq-sensors)). A LG passou a
> bloquear esse acesso por 24h ("use the official API"), então o app foi migrado.

## Como usar

1. Abra pelo atalho **Ar-condicionado LG** na Área de Trabalho (ou `pythonw app.py`).
2. Crie um token pessoal em <https://connect-pat.lgthinq.com>: entre com a conta LG,
   clique em **ADD NEW TOKEN**, marque todas as permissões e crie.
3. Cole o token no app.
4. O app fica no ícone da bandeja do sistema: clique nele para abrir ou fechar o painel.

Com o painel aberto o app lê o aparelho a cada 60 s; escondido na bandeja, a cada 10 min.

## Arquivos

- `app.py`: janela, ícone da bandeja e ponte com a interface
- `lg_backend.py`: conexão com a API oficial e comandos do aparelho
- `ui/index.html`: interface
- Configuração e log: `%USERPROFILE%\.arcondicionado-lg\`

Dependências: `pip install -r requirements.txt`
