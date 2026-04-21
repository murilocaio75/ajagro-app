import streamlit as st
import pandas as pd
from sqlalchemy import create_engine, text
from datetime import date
import io
import plotly.express as px
import plotly.graph_objects as go
from reportlab.lib.pagesizes import letter, A4
from reportlab.pdfgen import canvas
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib import colors

# --- CONFIGURAÇÃO DA PÁGINA ---
st.set_page_config(page_title="AJAGRO - Gestão Pecuária 3.3", layout="wide")

# ==============================================================
# FORMATAÇÃO NUMÉRICA BRASILEIRA (ponto milhar, vírgula decimal)
# ==============================================================
def fmt_br(valor, decimais=0):
    """Formata número no padrão brasileiro: 1.234,56"""
    if pd.isna(valor):
        return "0"
    fmt = f"{float(valor):,.{decimais}f}"
    return fmt.replace(",", "X").replace(".", ",").replace("X", ".")

def fmt_cab(valor):
    return f"{fmt_br(valor)} cab."

def fmt_brl(valor):
    return f"R$ {fmt_br(valor, 2)}"

# --- CONEXÃO ---
# Engine cacheada: evita criar nova conexão a cada rerun do Streamlit
@st.cache_resource
def get_engine():
    db_url = st.secrets["DB_CONN_STRING"].replace("postgres://", "postgresql+psycopg2://", 1)
    return create_engine(db_url)

def get_connection():
    return get_engine().connect()

# --- PARÂMETROS AJAGRO (metas configuráveis via interface) ---
@st.cache_data(ttl=300)
def get_parametros():
    """
    Retorna dict com todos os parâmetros configuráveis do Supabase.
    Estrutura: { 'chave': {'verde_max': float, 'amarelo_max': float, 'descricao': str} }
    Fallback hardcoded garante que o app não quebra se a tabela
    ainda não existir (útil durante o deploy inicial da v3.3).
    """
    FALLBACK = {
        "mort_bezerros":     {"verde_max": 3.0,  "amarelo_max": 5.0,  "descricao": "Sobrevivência na fase mais crítica da vida."},
        "mort_recria":       {"verde_max": 3.0,  "amarelo_max": 5.0,  "descricao": "Saúde dos animais em desenvolvimento."},
        "mort_novilhas":     {"verde_max": 1.0,  "amarelo_max": 2.0,  "descricao": "Fêmeas jovens aptas à reprodução."},
        "mort_vacas":        {"verde_max": 3.0,  "amarelo_max": 5.0,  "descricao": "Perda do ativo principal gerador de receita."},
        "comp_lact_vacas":   {"verde_max": 83.0, "amarelo_max": 75.0, "descricao": "% Lactantes / Total de Vacas"},
        "comp_lact_rebanho": {"verde_max": 55.0, "amarelo_max": 50.0, "descricao": "% Lactantes / Rebanho Total"},
    }
    try:
        conn = get_connection()
        df = pd.read_sql(text("SELECT chave, verde_max, amarelo_max, descricao FROM parametros_ajagro"), conn)
        conn.close()
        return {row["chave"]: {"verde_max": row["verde_max"],
                               "amarelo_max": row["amarelo_max"],
                               "descricao": row["descricao"]}
                for _, row in df.iterrows()}
    except Exception as e:
        st.warning(f"⚠️ Tabela parametros_ajagro não encontrada. Usando valores padrão. ({e})")
        return FALLBACK

# --- CATEGORIAS OFICIAIS ---
CATEGORIAS_LISTA = [
    "Vacas Lactantes", "Vacas Secas", "Vacas a refugar", "Vacas refugadas",
    "Mamando - Machos", "Mamando - Fêmeas", "Novilhas até 1 ano",
    "Novilhas de 1 a 2 anos", "Novilhas Prenhas", "Machos"
]

EVENTOS_ENTRADA = [
    "Entrada/Nascimento", "Entrada/Compras", "Entrada/Outros",
    "Entrada/Outros/Parcerias", "Transferências/De Outras Categorias",
    "Transferências/De Outras Fazendas"
]

EVENTOS_SAIDA = [
    "Saída/Vendas Comerciais", "Saída/Vendas Descartes", "Saída/Abates Comerciais",
    "Saída/Mortes", "Saída/Doações Extern", "Saída/Doações Intern",
    "Transferências/Para Outras Categorias", "Transferências/Para Outras Fazendas"
]

# ==============================================================
# FLUXO DE TRANSFERÊNCIAS AUTOMÁTICAS v3.3
# ==============================================================
FLUXO_TRANSFERENCIA = {
    "Mamando - Machos":       ["Machos"],
    "Mamando - Fêmeas":       ["Novilhas até 1 ano"],
    "Novilhas até 1 ano":     ["Novilhas de 1 a 2 anos"],
    "Novilhas de 1 a 2 anos": ["Novilhas Prenhas"],
    "Novilhas Prenhas":       ["Vacas Lactantes", "Vacas a refugar", "Vacas refugadas"],
    "Vacas Lactantes":        ["Vacas Secas", "Vacas a refugar", "Vacas refugadas"],
    "Vacas Secas":            ["Vacas Lactantes", "Vacas a refugar", "Vacas refugadas"],
    # ⏳ Fluxos de retrocesso — aguardando detalhes da Ajagro
    # "Novilhas Prenhas": += ["Novilhas de 1 a 2 anos"],  # aborto
}

# --- FUNÇÕES DE APOIO ---
def get_saldo_atual(conn, fazenda_id, categoria):
    query = text("""
        SELECT SUM(CASE WHEN evento LIKE 'Entrada%%' OR evento LIKE 'Transferências/De%%'
                        THEN quantidade ELSE -quantidade END)
        FROM lanc_estoque WHERE id_fazenda = :f AND categoria = :c
    """)
    res = conn.execute(query, {"f": fazenda_id, "c": categoria}).scalar()
    return res if res else 0

def get_historico_categoria(conn, fazenda_id, categoria, limite=5):
    """Retorna os últimos lançamentos de uma categoria para o histórico visual."""
    query = text("""
        SELECT TO_CHAR(data_movimento, 'DD/MM/YYYY') as data,
               evento, quantidade, observacao
        FROM lanc_estoque
        WHERE id_fazenda = :f AND categoria = :c
        ORDER BY id_lancamento DESC
        LIMIT :l
    """)
    return pd.read_sql(query, conn, params={"f": fazenda_id, "c": categoria, "l": limite})

def is_mes_fechado(conn, data_mov):
    primeiro_dia = data_mov.replace(day=1)
    query = text("""
        SELECT status FROM fechamentos_mensais
        WHERE DATE(ano_mes AT TIME ZONE 'UTC') = :d
          AND status = 'Fechado'
    """)
    try:
        res = conn.execute(query, {"d": primeiro_dia}).fetchone()
        return res is not None
    except Exception as e:
        st.warning(f"Aviso ao verificar fechamento mensal: {e}")
        return False

# --- LOGIN ---
if "autenticado" not in st.session_state:
    st.session_state["autenticado"] = False

if not st.session_state["autenticado"]:
    st.markdown("<h1 style='text-align: center;'>🔐 AJAGRO 3.3 - Acesso</h1>",
                unsafe_allow_html=True)
    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        with st.form("login"):
            u = st.text_input("Usuário")
            p = st.text_input("Senha", type="password")
            if st.form_submit_button("Entrar"):
                if u == st.secrets["USER_ADMIN"] and p == st.secrets["PASS_ADMIN"]:
                    st.session_state["autenticado"] = True
                    st.rerun()
                else:
                    st.error("Credenciais inválidas")
else:
    st.sidebar.title("Módulo MEG - AJAGRO")
    menu = st.sidebar.selectbox("Ir para:", [
        "Dashboard & Balanço",
        "📋 Módulo de Auditoria",
        "📊 KPIs Zootécnicos",
        "📄 Módulo de Relatórios",
        "Lançamento de Eventos",
        "Cadastros Base",
        "Fechamento Mensal",
        "⚙️ Parâmetros AJAGRO",
        "⚙️ Ajuste de Preços"
    ])

    if st.sidebar.button("🚪 Sair do Sistema"):
        st.session_state["autenticado"] = False
        st.rerun()

    # =========================================================
    # TELA: LANÇAMENTO DE EVENTOS
    # =========================================================
    if menu == "Lançamento de Eventos":
        st.header("📝 Registro de Eventos")
        conn = get_connection()
        fazendas = pd.read_sql(text("SELECT id_fazenda, nome_fazenda FROM fazendas"), conn)

        if fazendas.empty:
            st.warning("Cadastre uma fazenda primeiro em 'Cadastros Base'.")
        else:
            col1, col2 = st.columns(2)
            with col1:
                data_mov    = st.date_input("Data do Evento", date.today())
                faz_sel     = st.selectbox("Fazenda", fazendas['nome_fazenda'])
                evento_sel  = st.selectbox("Tipo de Evento", EVENTOS_ENTRADA + EVENTOS_SAIDA)
            with col2:
                if evento_sel == "Entrada/Nascimento":
                    cat_opcoes = ["Mamando - Machos", "Mamando - Fêmeas"]
                else:
                    cat_opcoes = CATEGORIAS_LISTA
                cat_sel = st.selectbox("Categoria", cat_opcoes)
                qtd     = st.number_input("Quantidade de Cabeças", min_value=1, step=1)
                obs     = st.text_area("Observações (Obrigatório para Nascimentos e Saídas)")

            id_f = int(fazendas[fazendas['nome_fazenda'] == faz_sel]['id_fazenda'].values[0])

            # --------------------------------------------------
            # HISTÓRICO VISUAL — saldo e últimos lançamentos
            # --------------------------------------------------
            saldo_atual = get_saldo_atual(conn, id_f, cat_sel)
            df_hist_cat = get_historico_categoria(conn, id_f, cat_sel)

            with st.expander(f"📊 Histórico de '{cat_sel}' — Saldo atual: {fmt_cab(saldo_atual)}", expanded=True):
                col_s1, col_s2 = st.columns([1, 2])
                with col_s1:
                    st.metric("Saldo Atual", fmt_cab(saldo_atual))

                    if evento_sel in EVENTOS_SAIDA or evento_sel == "Transferências/Para Outras Categorias":
                        saldo_depois = saldo_atual - qtd
                        delta_txt    = f"−{fmt_br(qtd)} cab."
                        delta_color  = "inverse"
                    elif evento_sel in EVENTOS_ENTRADA or evento_sel == "Transferências/De Outras Categorias":
                        saldo_depois = saldo_atual + qtd
                        delta_txt    = f"+{fmt_br(qtd)} cab."
                        delta_color  = "normal"
                    else:
                        saldo_depois = saldo_atual
                        delta_txt    = "—"
                        delta_color  = "off"

                    st.metric(
                        "Saldo após lançamento (previsão)",
                        fmt_cab(saldo_depois),
                        delta=delta_txt,
                        delta_color=delta_color
                    )

                with col_s2:
                    if not df_hist_cat.empty:
                        st.caption("Últimos lançamentos desta categoria:")
                        for _, row in df_hist_cat.iterrows():
                            eh_entrada = (
                                "Entrada" in str(row['evento']) or
                                "Transferências/De" in str(row['evento'])
                            )
                            icone = "🟢 +" if eh_entrada else "🔴 −"
                            st.markdown(
                                f"`{row['data']}` {icone}**{fmt_br(row['quantidade'])} cab.** "
                                f"— _{row['evento']}_"
                            )
                    else:
                        st.info("Nenhum lançamento anterior nesta categoria.")

            # --------------------------------------------------
            # SELETOR DE DESTINO para transferências com múltiplos destinos
            # --------------------------------------------------
            cat_destino_sel = None
            if evento_sel == "Transferências/Para Outras Categorias":
                destinos = FLUXO_TRANSFERENCIA.get(cat_sel, [])
                if len(destinos) == 0:
                    st.warning(f"⚠️ A categoria '{cat_sel}' não possui fluxo de destino definido.")
                elif len(destinos) == 1:
                    cat_destino_sel = destinos[0]
                    st.info(f"🔀 Destino automático: **{cat_destino_sel}**")
                else:
                    st.markdown("**🌿 Escolha o destino da transferência:**")
                    cols_dest = st.columns(len(destinos))
                    for i, dest in enumerate(destinos):
                        saldo_dest = get_saldo_atual(conn, id_f, dest)
                        with cols_dest[i]:
                            if st.button(
                                f"➡️ {dest}\n\nSaldo atual: {fmt_cab(saldo_dest)}",
                                key=f"dest_{i}",
                                use_container_width=True
                            ):
                                st.session_state["cat_destino_escolhido"] = dest
                    if "cat_destino_escolhido" in st.session_state:
                        cat_destino_sel = st.session_state["cat_destino_escolhido"]
                        st.success(f"✅ Destino selecionado: **{cat_destino_sel}**")

            # --------------------------------------------------
            # CONFIRMAR LANÇAMENTO
            # --------------------------------------------------
            if st.button("Confirmar Lançamento", type="primary"):
                if is_mes_fechado(conn, data_mov):
                    st.error(f"⛔ O mês {data_mov.strftime('%m/%Y')} já foi FECHADO.")
                elif (evento_sel == "Entrada/Nascimento" or evento_sel in EVENTOS_SAIDA) and not obs:
                    st.error("Para este evento, o campo observação é obrigatório.")
                else:
                    pode_gravar = True
                    if evento_sel in EVENTOS_SAIDA or evento_sel == "Transferências/Para Outras Categorias":
                        if saldo_atual < qtd:
                            st.error(f"Saldo insuficiente! Estoque de {cat_sel}: {fmt_cab(saldo_atual)}")
                            pode_gravar = False

                    if pode_gravar:
                        try:
                            if evento_sel == "Transferências/Para Outras Categorias":
                                if not cat_destino_sel:
                                    st.error("Selecione o destino da transferência antes de confirmar.")
                                else:
                                    saldo_origem_antes  = get_saldo_atual(conn, id_f, cat_sel)
                                    saldo_destino_antes = get_saldo_atual(conn, id_f, cat_destino_sel)

                                    sql_sai = text("INSERT INTO lanc_estoque (data_movimento, id_fazenda, quantidade, evento, categoria, observacao) VALUES (:d, :f, :q, :e, :c, :o)")
                                    conn.execute(sql_sai, {
                                        "d": data_mov, "f": id_f, "q": qtd,
                                        "e": "Transferências/Para Outras Categorias",
                                        "c": cat_sel,
                                        "o": f"Saída p/ {cat_destino_sel}. Obs: {obs}"
                                    })
                                    sql_ent = text("INSERT INTO lanc_estoque (data_movimento, id_fazenda, quantidade, evento, categoria, observacao) VALUES (:d, :f, :q, :e, :c, :o)")
                                    conn.execute(sql_ent, {
                                        "d": data_mov, "f": id_f, "q": qtd,
                                        "e": "Transferências/De Outras Categorias",
                                        "c": cat_destino_sel,
                                        "o": f"Entrada vinda de {cat_sel}. Obs: {obs}"
                                    })
                                    conn.commit()

                                    if "cat_destino_escolhido" in st.session_state:
                                        del st.session_state["cat_destino_escolhido"]

                                    saldo_origem_depois  = get_saldo_atual(conn, id_f, cat_sel)
                                    saldo_destino_depois = get_saldo_atual(conn, id_f, cat_destino_sel)

                                    st.success(f"✅ Transferência concluída! {fmt_cab(qtd)} movidas.")
                                    st.balloons()

                                    col_a, col_b = st.columns(2)
                                    with col_a:
                                        st.markdown("**📤 Origem**")
                                        st.metric(
                                            cat_sel,
                                            fmt_cab(saldo_origem_depois),
                                            delta=f"−{fmt_br(qtd)} cab.",
                                            delta_color="inverse"
                                        )
                                    with col_b:
                                        st.markdown("**📥 Destino**")
                                        st.metric(
                                            cat_destino_sel,
                                            fmt_cab(saldo_destino_depois),
                                            delta=f"+{fmt_br(qtd)} cab.",
                                            delta_color="normal"
                                        )
                            else:
                                sql = text("INSERT INTO lanc_estoque (data_movimento, id_fazenda, quantidade, evento, categoria, observacao) VALUES (:d, :f, :q, :e, :c, :o)")
                                conn.execute(sql, {
                                    "d": data_mov, "f": id_f, "q": qtd,
                                    "e": evento_sel, "c": cat_sel, "o": obs
                                })
                                conn.commit()
                                st.success(f"✅ {fmt_cab(qtd)} de '{cat_sel}' registrado como '{evento_sel}'.")
                                st.balloons()
                        except Exception as e:
                            st.error(f"Erro no banco: {e}")
        conn.close()

    # =========================================================
    # TELA: DASHBOARD & BALANÇO
    # =========================================================
    elif menu == "Dashboard & Balanço":
        st.header("📊 Balanço e Evolução Patrimonial")
        conn = get_connection()

        st.sidebar.divider()
        st.sidebar.subheader("Período do Dashboard")
        hoje  = date.today()
        d_ini = st.sidebar.date_input("Data Inicial", date(hoje.year, hoje.month, 1))
        d_fim = st.sidebar.date_input("Data Final", hoje)

        st.info(
            f"ℹ️ **Saldo acumulado histórico até {d_fim.strftime('%d/%m/%Y')}** "
            f"(todo o histórico desde o início). "
            f"Para ver movimentações por período, use o **📋 Módulo de Auditoria**."
        )

        q_balanco = text("""
            SELECT categoria as "Categoria",
                   SUM(CASE WHEN evento LIKE 'Entrada%%' OR evento LIKE 'Transferências/De%%'
                            THEN quantidade ELSE -quantidade END) as "Estoque"
            FROM lanc_estoque WHERE data_movimento <= :f
            GROUP BY categoria
        """)
        df_bal = pd.read_sql(q_balanco, conn, params={"f": d_fim})

        if not df_bal.empty:
            df_p    = pd.read_sql(text("SELECT * FROM precos_gestao"), conn)
            dict_p  = dict(zip(df_p['categoria'], df_p['valor']))
            df_bal['Preço Unit.'] = df_bal['Categoria'].map(dict_p).fillna(0)
            df_bal['Total R$']   = df_bal['Estoque'] * df_bal['Preço Unit.']
            df_bal = df_bal[df_bal['Estoque'] > 0]

            total_r = df_bal['Total R$'].sum()
            total_c = df_bal['Estoque'].sum()
            m1, m2, m3 = st.columns(3)
            m1.metric("Estoque Total",    fmt_cab(total_c))
            m2.metric("Valorização Total", fmt_brl(total_r))
            m3.metric("Média por Animal",  fmt_brl(total_r / total_c if total_c > 0 else 0))

            st.divider()
            col_g, col_t = st.columns([1, 1.2])
            with col_g:
                st.subheader("Distribuição do Patrimônio")
                fig = px.pie(df_bal[df_bal['Total R$'] > 0], values='Total R$',
                             names='Categoria', hole=0.4)
                st.plotly_chart(fig, use_container_width=True)
            with col_t:
                st.subheader("Tabela de Participação")
                df_bal_fmt = df_bal.copy()
                df_bal_fmt['Estoque']     = df_bal_fmt['Estoque'].apply(lambda v: fmt_br(v))
                df_bal_fmt['Preço Unit.'] = df_bal_fmt['Preço Unit.'].apply(fmt_brl)
                df_bal_fmt['Total R$']    = df_bal_fmt['Total R$'].apply(fmt_brl)
                st.dataframe(df_bal_fmt, use_container_width=True, hide_index=True)

                c_ex1, c_ex2 = st.columns(2)
                with c_ex1:
                    buf = io.BytesIO()
                    df_bal.to_excel(buf, index=False)
                    st.download_button("📥 Excel", buf.getvalue(), "balanco.xlsx")
                with c_ex2:
                    pdf_buf = io.BytesIO()
                    canv    = canvas.Canvas(pdf_buf, pagesize=letter)
                    canv.drawString(50, 750, f"AJAGRO - BALANÇO EM {d_fim.strftime('%d/%m/%Y')}")
                    y = 720
                    for _, row in df_bal.iterrows():
                        canv.drawString(50, y,
                            f"{row['Categoria']}: {fmt_cab(row['Estoque'])} - {fmt_brl(row['Total R$'])}")
                        y -= 20
                    canv.save()
                    st.download_button("📥 PDF", pdf_buf.getvalue(), "balanco.pdf")

            st.divider()
            st.subheader("📑 Últimos Lançamentos")
            df_hist = pd.read_sql(text("""
                SELECT TO_CHAR(data_movimento,'DD/MM/YYYY') as "Data",
                       evento as "Evento", categoria as "Categoria",
                       quantidade as "Quantidade"
                FROM lanc_estoque ORDER BY id_lancamento DESC LIMIT 20
            """), conn)
            df_hist["Quantidade"] = df_hist["Quantidade"].apply(lambda v: fmt_br(v))
            st.table(df_hist)
        else:
            st.warning("Nenhum lançamento encontrado até a data selecionada.")

        conn.close()

    # =========================================================
    # TELA: MÓDULO DE AUDITORIA
    # =========================================================
    elif menu == "📋 Módulo de Auditoria":
        st.header("📋 Módulo de Auditoria — Extrato de Movimentações")

        conn     = get_connection()
        fazendas = pd.read_sql(text("SELECT id_fazenda, nome_fazenda FROM fazendas"), conn)

        st.sidebar.divider()
        st.sidebar.subheader("Filtros da Auditoria")
        hoje  = date.today()
        d_ini = st.sidebar.date_input("Data Inicial", date(hoje.year, hoje.month, 1), key="aud_ini")
        d_fim = st.sidebar.date_input("Data Final", hoje, key="aud_fim")

        faz_opcoes = ["Todas as Fazendas"] + fazendas['nome_fazenda'].tolist()
        faz_sel    = st.sidebar.selectbox("Fazenda",   faz_opcoes,      key="aud_faz")
        cat_opcoes = ["Todas as Categorias"] + CATEGORIAS_LISTA
        cat_sel    = st.sidebar.selectbox("Categoria", cat_opcoes,      key="aud_cat")

        filtro_faz_sql = ""
        filtro_cat_sql = ""
        params_base    = {"d_ini": d_ini, "d_fim": d_fim}

        if faz_sel != "Todas as Fazendas":
            id_faz = int(fazendas[fazendas['nome_fazenda'] == faz_sel]['id_fazenda'].values[0])
            filtro_faz_sql       = "AND id_fazenda = :id_faz"
            params_base["id_faz"] = id_faz

        if cat_sel != "Todas as Categorias":
            filtro_cat_sql    = "AND categoria = :cat"
            params_base["cat"] = cat_sel

        # Saldo inicial
        q_saldo_ini = text(f"""
            SELECT categoria,
                   SUM(CASE WHEN evento LIKE 'Entrada%%' OR evento LIKE 'Transferências/De%%'
                            THEN quantidade ELSE -quantidade END) as saldo_inicial
            FROM lanc_estoque
            WHERE data_movimento < :d_ini
            {filtro_faz_sql} {filtro_cat_sql}
            GROUP BY categoria
        """)
        df_saldo_ini = pd.read_sql(q_saldo_ini, conn, params=params_base)

        # Movimentações do período
        q_mov = text(f"""
            SELECT categoria,
                   SUM(CASE WHEN evento LIKE 'Entrada%%' OR evento LIKE 'Transferências/De%%'
                            THEN quantidade ELSE 0 END) as entradas,
                   SUM(CASE WHEN evento LIKE 'Saída%%' OR evento LIKE 'Transferências/Para%%'
                            THEN quantidade ELSE 0 END) as saidas
            FROM lanc_estoque
            WHERE data_movimento BETWEEN :d_ini AND :d_fim
            {filtro_faz_sql} {filtro_cat_sql}
            GROUP BY categoria
        """)
        df_mov = pd.read_sql(q_mov, conn, params=params_base)

        # Consolidado
        todas_cats = pd.DataFrame({'categoria': CATEGORIAS_LISTA if cat_sel == "Todas as Categorias" else [cat_sel]})
        df_cons    = todas_cats.merge(df_saldo_ini, on='categoria', how='left')
        df_cons    = df_cons.merge(df_mov, on='categoria', how='left')
        df_cons    = df_cons.fillna(0)
        df_cons['saldo_final'] = df_cons['saldo_inicial'] + df_cons['entradas'] - df_cons['saidas']
        df_cons = df_cons[(df_cons['saldo_inicial'] != 0) |
                          (df_cons['entradas'] != 0) |
                          (df_cons['saidas'] != 0)]

        # KPIs
        total_ini = int(df_cons['saldo_inicial'].sum())
        total_ent = int(df_cons['entradas'].sum())
        total_sai = int(df_cons['saidas'].sum())
        total_fim = int(df_cons['saldo_final'].sum())

        st.subheader(f"Resumo do Período: {d_ini.strftime('%d/%m/%Y')} → {d_fim.strftime('%d/%m/%Y')}")
        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Saldo Inicial",       fmt_cab(total_ini), help="Acumulado histórico antes do período")
        k2.metric("Entradas no Período", f"+ {fmt_br(total_ent)} cab.")
        k3.metric("Saídas no Período",   f"- {fmt_br(total_sai)} cab.")
        k4.metric("Saldo Final",         fmt_cab(total_fim),
                  delta=f"{total_fim - total_ini:+,.0f} cab.".replace(",", "."))

        st.divider()

        # Tabela por categoria
        st.subheader("📊 Saldo por Categoria")
        df_display = df_cons.rename(columns={
            'categoria':     'Categoria',
            'saldo_inicial': 'Saldo Inicial',
            'entradas':      'Entradas',
            'saidas':        'Saídas',
            'saldo_final':   'Saldo Final'
        })

        def highlight_saldo(val):
            if isinstance(val, (int, float)) and val < 0:
                return 'color: red; font-weight: bold'
            return ''

        st.dataframe(
            df_display.style
                .map(highlight_saldo, subset=['Saldo Final'])
                .format({
                    'Saldo Inicial': lambda v: fmt_br(v),
                    'Entradas':      lambda v: fmt_br(v),
                    'Saídas':        lambda v: fmt_br(v),
                    'Saldo Final':   lambda v: fmt_br(v),
                }),
            use_container_width=True,
            hide_index=True
        )

        st.divider()

        # Gráfico
        st.subheader("📈 Movimentações do Período por Categoria")
        df_graf = df_cons[df_cons['entradas'] + df_cons['saidas'] > 0]
        if not df_graf.empty:
            fig_bar = go.Figure()
            fig_bar.add_trace(go.Bar(name='Entradas', x=df_graf['categoria'],
                                     y=df_graf['entradas'], marker_color='#2ecc71'))
            fig_bar.add_trace(go.Bar(name='Saídas',   x=df_graf['categoria'],
                                     y=df_graf['saidas'],   marker_color='#e74c3c'))
            fig_bar.update_layout(barmode='group', xaxis_tickangle=-30,
                                  legend=dict(orientation="h"), margin=dict(b=80))
            st.plotly_chart(fig_bar, use_container_width=True)
        else:
            st.info("Nenhuma movimentação registrada neste período para os filtros selecionados.")

        st.divider()

        # Extrato detalhado
        st.subheader("📑 Extrato Detalhado de Lançamentos")
        q_extrato = text(f"""
            SELECT
                TO_CHAR(l.data_movimento, 'DD/MM/YYYY') as "Data",
                f.nome_fazenda                          as "Fazenda",
                l.evento                                as "Evento",
                l.categoria                             as "Categoria",
                l.quantidade                            as "Qtd",
                CASE WHEN l.evento LIKE 'Entrada%%' OR l.evento LIKE 'Transferências/De%%'
                     THEN l.quantidade ELSE 0 END       as "Entrada (+)",
                CASE WHEN l.evento LIKE 'Saída%%' OR l.evento LIKE 'Transferências/Para%%'
                     THEN l.quantidade ELSE 0 END       as "Saída (-)",
                l.observacao                            as "Observação"
            FROM lanc_estoque l
            JOIN fazendas f ON f.id_fazenda = l.id_fazenda
            WHERE l.data_movimento BETWEEN :d_ini AND :d_fim
            {filtro_faz_sql} {filtro_cat_sql}
            ORDER BY l.data_movimento ASC, l.id_lancamento ASC
        """)
        df_extrato = pd.read_sql(q_extrato, conn, params=params_base)

        if not df_extrato.empty:
            df_extrato_fmt = df_extrato.copy()
            for col in ["Qtd", "Entrada (+)", "Saída (-)"]:
                df_extrato_fmt[col] = df_extrato_fmt[col].apply(lambda v: fmt_br(v))
            st.dataframe(df_extrato_fmt, use_container_width=True, hide_index=True)

            col_exp1, col_exp2 = st.columns(2)
            with col_exp1:
                buf_xls = io.BytesIO()
                with pd.ExcelWriter(buf_xls, engine='openpyxl') as writer:
                    df_display.to_excel(writer, sheet_name='Saldo por Categoria', index=False)
                    df_extrato.to_excel(writer, sheet_name='Extrato Detalhado', index=False)
                st.download_button("📥 Exportar Excel (Auditoria)",
                                   buf_xls.getvalue(), "auditoria.xlsx",
                                   mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            with col_exp2:
                pdf_buf = io.BytesIO()
                doc     = SimpleDocTemplate(pdf_buf, pagesize=A4,
                                            leftMargin=30, rightMargin=30,
                                            topMargin=40, bottomMargin=30)
                styles   = getSampleStyleSheet()
                elements = []
                elements.append(Paragraph("AJAGRO — Extrato de Auditoria", styles['Title']))
                elements.append(Paragraph(
                    f"Período: {d_ini.strftime('%d/%m/%Y')} a {d_fim.strftime('%d/%m/%Y')} | "
                    f"Fazenda: {faz_sel} | Categoria: {cat_sel}", styles['Normal']))
                elements.append(Spacer(1, 12))
                elements.append(Paragraph("Saldo por Categoria", styles['Heading2']))
                data_tab = [['Categoria', 'Saldo Ini.', 'Entradas', 'Saídas', 'Saldo Final']]
                for _, row in df_cons.iterrows():
                    data_tab.append([
                        row['categoria'],
                        fmt_br(row['saldo_inicial']),
                        fmt_br(row['entradas']),
                        fmt_br(row['saidas']),
                        fmt_br(row['saldo_final'])
                    ])
                t = Table(data_tab, hAlign='LEFT')
                t.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2c3e50')),
                    ('TEXTCOLOR',  (0, 0), (-1, 0), colors.white),
                    ('FONTSIZE',   (0, 0), (-1, -1), 8),
                    ('GRID',       (0, 0), (-1, -1), 0.5, colors.grey),
                    ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f2f2f2')]),
                ]))
                elements.append(t)
                elements.append(Spacer(1, 16))
                elements.append(Paragraph("Extrato Detalhado", styles['Heading2']))
                cols_pdf = ['Data', 'Fazenda', 'Categoria', 'Evento', 'Entrada (+)', 'Saída (-)']
                data_ext = [cols_pdf]
                for _, row in df_extrato.iterrows():
                    data_ext.append([
                        row['Data'], row['Fazenda'], row['Categoria'],
                        row['Evento'][:30],
                        fmt_br(row['Entrada (+)']),
                        fmt_br(row['Saída (-)'])
                    ])
                t2 = Table(data_ext, hAlign='LEFT', colWidths=[50, 70, 90, 130, 50, 50])
                t2.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#27ae60')),
                    ('TEXTCOLOR',  (0, 0), (-1, 0), colors.white),
                    ('FONTSIZE',   (0, 0), (-1, -1), 7),
                    ('GRID',       (0, 0), (-1, -1), 0.4, colors.grey),
                    ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#eafaf1')]),
                ]))
                elements.append(t2)
                doc.build(elements)
                st.download_button("📥 Exportar PDF (Auditoria)",
                                   pdf_buf.getvalue(), "auditoria.pdf",
                                   mime="application/pdf")
        else:
            st.info("Nenhum lançamento encontrado neste período para os filtros selecionados.")

        conn.close()

    # =========================================================
    # TELA: MÓDULO DE RELATÓRIOS
    # =========================================================
    elif menu == "📄 Módulo de Relatórios":
        st.header("📄 Relatório Gerencial Consolidado")
        st.caption("Consolida Balanço, Movimentações, KPIs Zootécnicos e Fechamentos em um único relatório exportável.")

        conn     = get_connection()
        fazendas = pd.read_sql(text("SELECT id_fazenda, nome_fazenda FROM fazendas"), conn)

        st.sidebar.divider()
        st.sidebar.subheader("Filtros do Relatório")
        hoje  = date.today()
        d_ini = st.sidebar.date_input("Data Inicial", date(hoje.year, hoje.month, 1), key="rel_ini")
        d_fim = st.sidebar.date_input("Data Final",   hoje,                           key="rel_fim")

        faz_opcoes = ["Todas as Fazendas"] + fazendas['nome_fazenda'].tolist()
        faz_sel    = st.sidebar.selectbox("Fazenda", faz_opcoes, key="rel_faz")

        cat_opcoes = ["Todas as Categorias"] + CATEGORIAS_LISTA
        cat_sel    = st.sidebar.selectbox("Categoria", cat_opcoes, key="rel_cat")

        evt_opcoes = ["Todos os Eventos"] + EVENTOS_ENTRADA + EVENTOS_SAIDA
        evt_sel    = st.sidebar.selectbox("Tipo de Evento", evt_opcoes, key="rel_evt")

        filtro_faz_sql     = ""
        filtro_faz_sql_bare = ""
        filtro_cat_sql     = ""
        filtro_cat_sql_bare = ""
        filtro_evt_sql     = ""
        params_base        = {"d_ini": d_ini, "d_fim": d_fim, "d_fim_bal": d_fim}

        if faz_sel != "Todas as Fazendas":
            id_faz = int(fazendas[fazendas['nome_fazenda'] == faz_sel]['id_fazenda'].values[0])
            filtro_faz_sql        = "AND l.id_fazenda = :id_faz"
            filtro_faz_sql_bare   = "AND id_fazenda = :id_faz"
            params_base["id_faz"] = id_faz

        if cat_sel != "Todas as Categorias":
            filtro_cat_sql        = "AND l.categoria = :cat"
            filtro_cat_sql_bare   = "AND categoria = :cat"
            params_base["cat"]    = cat_sel

        if evt_sel != "Todos os Eventos":
            filtro_evt_sql      = "AND l.evento = :evt"
            params_base["evt"]  = evt_sel

        st.info(
            f"📅 **Período:** {d_ini.strftime('%d/%m/%Y')} → {d_fim.strftime('%d/%m/%Y')}  |  "
            f"🏠 **Fazenda:** {faz_sel}  |  "
            f"🐄 **Categoria:** {cat_sel}  |  "
            f"📋 **Evento:** {evt_sel}"
        )

        # --- SEÇÃO 1: BALANÇO PATRIMONIAL ---
        st.subheader("1. Balanço Patrimonial")
        st.caption(f"Estoque acumulado até {d_fim.strftime('%d/%m/%Y')}")

        q_bal = text(f"""
            SELECT categoria as "Categoria",
                   SUM(CASE WHEN evento LIKE 'Entrada%%' OR evento LIKE 'Transferências/De%%'
                            THEN quantidade ELSE -quantidade END) as "Estoque"
            FROM lanc_estoque
            WHERE data_movimento <= :d_fim_bal
            {filtro_faz_sql_bare} {filtro_cat_sql_bare}
            GROUP BY categoria
        """)
        df_bal = pd.read_sql(q_bal, conn, params=params_base)
        df_p   = pd.read_sql(text("SELECT * FROM precos_gestao"), conn)
        dict_p = dict(zip(df_p['categoria'], df_p['valor']))

        if not df_bal.empty:
            df_bal['Preço Unit.'] = df_bal['Categoria'].map(dict_p).fillna(0)
            df_bal['Total R$']    = df_bal['Estoque'] * df_bal['Preço Unit.']
            df_bal = df_bal[df_bal['Estoque'] > 0].copy()
            total_cab = df_bal['Estoque'].sum()
            total_val = df_bal['Total R$'].sum()
            m1, m2, m3 = st.columns(3)
            m1.metric("Estoque Total",     fmt_cab(total_cab))
            m2.metric("Valorização Total", fmt_brl(total_val))
            m3.metric("Média por Animal",  fmt_brl(total_val / total_cab if total_cab > 0 else 0))
            df_bal_fmt = df_bal.copy()
            df_bal_fmt['Estoque']     = df_bal_fmt['Estoque'].apply(fmt_br)
            df_bal_fmt['Preço Unit.'] = df_bal_fmt['Preço Unit.'].apply(fmt_brl)
            df_bal_fmt['Total R$']    = df_bal_fmt['Total R$'].apply(fmt_brl)
            st.dataframe(df_bal_fmt, use_container_width=True, hide_index=True)
        else:
            st.info("Sem dados de balanço para os filtros selecionados.")
            df_bal    = pd.DataFrame()
            total_cab = 0
            total_val = 0.0

        # --- SEÇÃO 2: MOVIMENTAÇÕES DO PERÍODO ---
        st.divider()
        st.subheader("2. Movimentações do Período")

        q_mov_det = text(f"""
            SELECT
                TO_CHAR(l.data_movimento, 'DD/MM/YYYY') as "Data",
                f.nome_fazenda                          as "Fazenda",
                l.categoria                             as "Categoria",
                l.evento                                as "Evento",
                CASE WHEN l.evento LIKE 'Entrada%%' OR l.evento LIKE 'Transferências/De%%'
                     THEN l.quantidade ELSE 0 END       as "Entrada (+)",
                CASE WHEN l.evento LIKE 'Saída%%' OR l.evento LIKE 'Transferências/Para%%'
                     THEN l.quantidade ELSE 0 END       as "Saída (-)",
                l.observacao                            as "Observação"
            FROM lanc_estoque l
            JOIN fazendas f ON f.id_fazenda = l.id_fazenda
            WHERE l.data_movimento BETWEEN :d_ini AND :d_fim
            {filtro_faz_sql} {filtro_cat_sql} {filtro_evt_sql}
            ORDER BY l.data_movimento ASC, l.id_lancamento ASC
        """)
        df_mov = pd.read_sql(q_mov_det, conn, params=params_base)

        q_saldo_cat = text(f"""
            SELECT categoria as "Categoria",
                   SUM(CASE WHEN evento LIKE 'Entrada%%' OR evento LIKE 'Transferências/De%%'
                            THEN quantidade ELSE -quantidade END) as saldo_ini,
                   SUM(CASE WHEN (evento LIKE 'Entrada%%' OR evento LIKE 'Transferências/De%%')
                                 AND data_movimento BETWEEN :d_ini AND :d_fim
                            THEN quantidade ELSE 0 END) as entradas,
                   SUM(CASE WHEN (evento LIKE 'Saída%%' OR evento LIKE 'Transferências/Para%%')
                                 AND data_movimento BETWEEN :d_ini AND :d_fim
                            THEN quantidade ELSE 0 END) as saidas
            FROM lanc_estoque
            WHERE 1=1 {filtro_faz_sql_bare} {filtro_cat_sql_bare}
            GROUP BY categoria
        """)
        df_saldo_cat = pd.read_sql(q_saldo_cat, conn, params=params_base)

        if not df_saldo_cat.empty:
            df_saldo_cat['Saldo Inicial'] = df_saldo_cat['saldo_ini'] - df_saldo_cat['entradas'] + df_saldo_cat['saidas']
            df_saldo_cat['Saldo Final']   = df_saldo_cat['Saldo Inicial'] + df_saldo_cat['entradas'] - df_saldo_cat['saidas']
            df_saldo_cat = df_saldo_cat.rename(columns={'entradas': 'Entradas', 'saidas': 'Saídas'})[
                ['Categoria', 'Saldo Inicial', 'Entradas', 'Saídas', 'Saldo Final']
            ]
            df_saldo_cat = df_saldo_cat[
                (df_saldo_cat['Saldo Inicial'] != 0) |
                (df_saldo_cat['Entradas'] != 0) |
                (df_saldo_cat['Saídas'] != 0)
            ]
            total_ent = int(df_saldo_cat['Entradas'].sum())
            total_sai = int(df_saldo_cat['Saídas'].sum())
            m1, m2 = st.columns(2)
            m1.metric("Total Entradas", f"+ {fmt_br(total_ent)} cab.")
            m2.metric("Total Saídas",   f"- {fmt_br(total_sai)} cab.")
            df_sc_fmt = df_saldo_cat.copy()
            for col in ['Saldo Inicial', 'Entradas', 'Saídas', 'Saldo Final']:
                df_sc_fmt[col] = df_sc_fmt[col].apply(fmt_br)
            st.dataframe(df_sc_fmt, use_container_width=True, hide_index=True)
        else:
            df_saldo_cat = pd.DataFrame()
            total_ent    = 0
            total_sai    = 0

        if not df_mov.empty:
            with st.expander("📑 Extrato linha a linha", expanded=False):
                df_mov_fmt = df_mov.copy()
                df_mov_fmt['Entrada (+)'] = df_mov_fmt['Entrada (+)'].apply(fmt_br)
                df_mov_fmt['Saída (-)']   = df_mov_fmt['Saída (-)'].apply(fmt_br)
                st.dataframe(df_mov_fmt, use_container_width=True, hide_index=True)
        else:
            st.info("Nenhuma movimentação encontrada para os filtros selecionados.")

        # --- SEÇÃO 3: KPIs ZOOTÉCNICOS ---
        st.divider()
        st.subheader("3. KPIs Zootécnicos")

        _p = get_parametros()

        def _calc_kpi_rel(conn, categorias, params, filtro_faz_bare):
            cats_sql  = ", ".join([f"'{c}'" for c in categorias])
            obitos    = conn.execute(text(f"""
                SELECT COALESCE(SUM(quantidade), 0) FROM lanc_estoque
                WHERE evento = 'Saída/Mortes' AND categoria IN ({cats_sql})
                  AND data_movimento BETWEEN :d_ini AND :d_fim {filtro_faz_bare}
            """), params).scalar() or 0
            saldo_ini = conn.execute(text(f"""
                SELECT COALESCE(SUM(CASE WHEN evento LIKE 'Entrada%%' OR evento LIKE 'Transferências/De%%'
                     THEN quantidade ELSE -quantidade END), 0)
                FROM lanc_estoque
                WHERE categoria IN ({cats_sql}) AND data_movimento < :d_ini {filtro_faz_bare}
            """), params).scalar() or 0
            entradas  = conn.execute(text(f"""
                SELECT COALESCE(SUM(quantidade), 0) FROM lanc_estoque
                WHERE (evento LIKE 'Entrada%%' OR evento LIKE 'Transferências/De%%')
                  AND categoria IN ({cats_sql})
                  AND data_movimento BETWEEN :d_ini AND :d_fim {filtro_faz_bare}
            """), params).scalar() or 0
            pop  = saldo_ini + entradas
            taxa = round((obitos / pop * 100) if pop > 0 else 0.0, 2)
            return int(obitos), int(pop), taxa

        grupos_kpi_rel = {
            "Bezerros (0–60 dias)":  {"categorias": ["Mamando - Machos", "Mamando - Fêmeas"],
                                       "verde_max": _p["mort_bezerros"]["verde_max"], "amarelo_max": _p["mort_bezerros"]["amarelo_max"]},
            "Recria (2–12 meses)":   {"categorias": ["Novilhas até 1 ano"],
                                       "verde_max": _p["mort_recria"]["verde_max"],   "amarelo_max": _p["mort_recria"]["amarelo_max"]},
            "Novilhas (>12 meses)":  {"categorias": ["Novilhas de 1 a 2 anos", "Novilhas Prenhas"],
                                       "verde_max": _p["mort_novilhas"]["verde_max"], "amarelo_max": _p["mort_novilhas"]["amarelo_max"]},
            "Vacas Adultas":         {"categorias": ["Vacas Lactantes", "Vacas Secas", "Vacas a refugar", "Vacas refugadas"],
                                       "verde_max": _p["mort_vacas"]["verde_max"],    "amarelo_max": _p["mort_vacas"]["amarelo_max"]},
        }

        rows_kpi = []
        for nome, cfg in grupos_kpi_rel.items():
            ob, pop, taxa = _calc_kpi_rel(conn, cfg["categorias"], params_base, filtro_faz_sql_bare)
            if taxa < cfg["verde_max"]:       status = "🟢 IDEAL"
            elif taxa <= cfg["amarelo_max"]:  status = "🟡 ATENÇÃO"
            else:                             status = "🔴 CRÍTICO"
            rows_kpi.append({"Grupo": nome, "Óbitos": fmt_br(ob), "População": fmt_br(pop),
                              "Taxa (%)": fmt_br(taxa, 2), "Status": status,
                              "Meta Verde": f"< {fmt_br(cfg['verde_max'], 1)}%",
                              "Limite Crítico": f"> {fmt_br(cfg['amarelo_max'], 1)}%"})

        q_comp = text(f"""
            SELECT categoria, SUM(CASE WHEN evento LIKE 'Entrada%%' OR evento LIKE 'Transferências/De%%'
                            THEN quantidade ELSE -quantidade END) as saldo
            FROM lanc_estoque WHERE data_movimento <= :d_fim_bal {filtro_faz_sql_bare}
            GROUP BY categoria
        """)
        df_comp    = pd.read_sql(q_comp, conn, params=params_base)
        saldo_dict = dict(zip(df_comp['categoria'], df_comp['saldo'].clip(lower=0))) if not df_comp.empty else {}
        lactantes     = saldo_dict.get("Vacas Lactantes", 0)
        secas         = saldo_dict.get("Vacas Secas", 0)
        total_rebanho = sum(v for v in saldo_dict.values() if v > 0)
        total_vacas   = lactantes + secas
        pct_lv = (lactantes / total_vacas   * 100) if total_vacas   > 0 else 0.0
        pct_lr = (lactantes / total_rebanho * 100) if total_rebanho > 0 else 0.0

        def _sem_comp(v, vmin, amin):
            if v >= vmin:   return "🟢 IDEAL"
            elif v >= amin: return "🟡 ATENÇÃO"
            else:           return "🔴 CRÍTICO"

        rows_kpi.append({"Grupo": "% Lactantes / Total Vacas",    "Óbitos": "—", "População": fmt_br(total_vacas),
                          "Taxa (%)": fmt_br(pct_lv, 2), "Status": _sem_comp(pct_lv, _p["comp_lact_vacas"]["verde_max"],   _p["comp_lact_vacas"]["amarelo_max"]),
                          "Meta Verde": f"≥ {fmt_br(_p['comp_lact_vacas']['verde_max'], 1)}%",   "Limite Crítico": f"< {fmt_br(_p['comp_lact_vacas']['amarelo_max'], 1)}%"})
        rows_kpi.append({"Grupo": "% Lactantes / Rebanho Total",  "Óbitos": "—", "População": fmt_br(total_rebanho),
                          "Taxa (%)": fmt_br(pct_lr, 2), "Status": _sem_comp(pct_lr, _p["comp_lact_rebanho"]["verde_max"], _p["comp_lact_rebanho"]["amarelo_max"]),
                          "Meta Verde": f"≥ {fmt_br(_p['comp_lact_rebanho']['verde_max'], 1)}%", "Limite Crítico": f"< {fmt_br(_p['comp_lact_rebanho']['amarelo_max'], 1)}%"})

        df_kpi = pd.DataFrame(rows_kpi)
        st.dataframe(df_kpi, use_container_width=True, hide_index=True)

        # --- SEÇÃO 4: FECHAMENTOS MENSAIS ---
        st.divider()
        st.subheader("4. Fechamentos Mensais no Período")
        try:
            df_fech = pd.read_sql(text("""
                SELECT TO_CHAR(DATE(ano_mes AT TIME ZONE 'UTC'), 'MM/YYYY') as "Mês",
                       status as "Status",
                       TO_CHAR(updated_at, 'DD/MM/YYYY HH24:MI') as "Registrado em"
                FROM fechamentos_mensais
                WHERE DATE(ano_mes AT TIME ZONE 'UTC') BETWEEN :d_ini AND :d_fim
                ORDER BY ano_mes ASC
            """), conn, params={"d_ini": d_ini, "d_fim": d_fim})
        except Exception:
            df_fech = pd.DataFrame(columns=["Mês", "Status", "Registrado em"])

        if not df_fech.empty:
            n_fechados = (df_fech["Status"] == "Fechado").sum()
            st.metric("Meses fechados no período", f"{n_fechados} / {len(df_fech)}")
            st.dataframe(df_fech, use_container_width=True, hide_index=True)
        else:
            st.info("Nenhum fechamento registrado neste período.")

        # --- EXPORTAÇÕES ---
        st.divider()
        st.subheader("📥 Exportar Relatório")
        col_pdf, col_xls = st.columns(2)

        # EXCEL — 4 abas
        with col_xls:
            buf_xls = io.BytesIO()
            with pd.ExcelWriter(buf_xls, engine='openpyxl') as writer:
                if not df_bal.empty:
                    df_bal_exp = df_bal.copy()
                    df_bal_exp['Estoque']     = df_bal_exp['Estoque'].apply(fmt_br)
                    df_bal_exp['Preço Unit.'] = df_bal_exp['Preço Unit.'].apply(fmt_brl)
                    df_bal_exp['Total R$']    = df_bal_exp['Total R$'].apply(fmt_brl)
                    df_bal_exp.to_excel(writer, sheet_name='1. Balanço Patrimonial', index=False)
                else:
                    pd.DataFrame([{"Info": "Sem dados"}]).to_excel(writer, sheet_name='1. Balanço Patrimonial', index=False)

                if not df_saldo_cat.empty:
                    df_saldo_cat.to_excel(writer, sheet_name='2. Movimentações', index=False, startrow=0)
                if not df_mov.empty:
                    start = len(df_saldo_cat) + 3 if not df_saldo_cat.empty else 0
                    df_mov.to_excel(writer, sheet_name='2. Movimentações', index=False, startrow=start)

                df_kpi.to_excel(writer, sheet_name='3. KPIs Zootécnicos', index=False)

                if not df_fech.empty:
                    df_fech.to_excel(writer, sheet_name='4. Fechamentos Mensais', index=False)
                else:
                    pd.DataFrame([{"Info": "Sem fechamentos no período"}]).to_excel(
                        writer, sheet_name='4. Fechamentos Mensais', index=False)

            st.download_button(
                "📊 Baixar Excel (4 abas)",
                buf_xls.getvalue(),
                f"relatorio_ajagro_{d_ini.strftime('%Y%m%d')}_{d_fim.strftime('%Y%m%d')}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True
            )

        # PDF — ReportLab com capa + 4 seções
        with col_pdf:
            from reportlab.lib.styles import ParagraphStyle
            from reportlab.lib.enums import TA_CENTER
            from reportlab.lib.units import cm
            from reportlab.platypus import HRFlowable

            pdf_buf = io.BytesIO()
            doc_pdf = SimpleDocTemplate(pdf_buf, pagesize=A4,
                                        leftMargin=40, rightMargin=40,
                                        topMargin=50, bottomMargin=40)

            style_title   = ParagraphStyle('T2',  parent=styles['Title'],   fontSize=18, spaceAfter=6,  alignment=TA_CENTER)
            style_sub_pdf = ParagraphStyle('Sub', parent=styles['Normal'],  fontSize=10, spaceAfter=12, alignment=TA_CENTER, textColor=colors.HexColor('#555555'))
            style_h2_pdf  = ParagraphStyle('H2',  parent=styles['Heading2'],fontSize=12, spaceBefore=14,spaceAfter=6,  textColor=colors.HexColor('#2c3e50'))
            style_cap_pdf = ParagraphStyle('Cap', parent=styles['Normal'],  fontSize=8,  spaceAfter=6,  textColor=colors.HexColor('#777777'))
            style_nor_pdf = ParagraphStyle('Nor', parent=styles['Normal'],  fontSize=9,  spaceAfter=4)
            styles_obj    = getSampleStyleSheet()

            COL_HDR   = colors.HexColor('#2c3e50')
            COL_ALT   = colors.HexColor('#f2f2f2')
            COL_GREEN_PDF = colors.HexColor('#27ae60')

            def make_pdf_table(data, col_widths=None, hdr_color=COL_HDR):
                t = Table(data, colWidths=col_widths, hAlign='LEFT', repeatRows=1)
                t.setStyle(TableStyle([
                    ('BACKGROUND',    (0,0),(-1,0),  hdr_color),
                    ('TEXTCOLOR',     (0,0),(-1,0),  colors.white),
                    ('FONTNAME',      (0,0),(-1,0),  'Helvetica-Bold'),
                    ('FONTSIZE',      (0,0),(-1,-1), 7.5),
                    ('ROWBACKGROUNDS',(0,1),(-1,-1), [colors.white, COL_ALT]),
                    ('GRID',          (0,0),(-1,-1), 0.4, colors.HexColor('#cccccc')),
                    ('LEFTPADDING',   (0,0),(-1,-1), 5),
                    ('RIGHTPADDING',  (0,0),(-1,-1), 5),
                    ('TOPPADDING',    (0,0),(-1,-1), 3),
                    ('BOTTOMPADDING', (0,0),(-1,-1), 3),
                ]))
                return t

            elem = []

            # Capa
            elem.append(Spacer(1, 2*cm))
            elem.append(Paragraph("AJAGRO", style_title))
            elem.append(Paragraph("Relatório Gerencial Consolidado", style_title))
            elem.append(Spacer(1, 0.4*cm))
            elem.append(Paragraph(f"Período: {d_ini.strftime('%d/%m/%Y')} a {d_fim.strftime('%d/%m/%Y')}", style_sub_pdf))
            elem.append(Paragraph(f"Fazenda: {faz_sel}  |  Categoria: {cat_sel}  |  Evento: {evt_sel}", style_sub_pdf))
            elem.append(Paragraph(f"Gerado em: {date.today().strftime('%d/%m/%Y')}", style_sub_pdf))
            elem.append(Spacer(1, 1*cm))
            elem.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor('#2c3e50')))
            elem.append(Spacer(1, 1*cm))

            # Seção 1 — Balanço
            elem.append(Paragraph("1. Balanço Patrimonial", style_h2_pdf))
            elem.append(Paragraph(
                f"Estoque até {d_fim.strftime('%d/%m/%Y')} | Total: {fmt_cab(total_cab)} | Valorização: {fmt_brl(total_val)}",
                style_cap_pdf
            ))
            if not df_bal.empty:
                data_b = [['Categoria', 'Estoque (cab.)', 'Preço Unit.', 'Total R$']]
                for _, row in df_bal.iterrows():
                    data_b.append([row['Categoria'], fmt_br(row['Estoque']), fmt_brl(row['Preço Unit.']), fmt_brl(row['Total R$'])])
                data_b.append(['TOTAL', fmt_br(total_cab), '—', fmt_brl(total_val)])
                tb = make_pdf_table(data_b, col_widths=[150, 80, 80, 85])
                tb.setStyle(TableStyle([
                    ('FONTNAME',   (0, len(data_b)-1), (-1, len(data_b)-1), 'Helvetica-Bold'),
                    ('BACKGROUND', (0, len(data_b)-1), (-1, len(data_b)-1), colors.HexColor('#e8f4f8')),
                ]))
                elem.append(tb)
            else:
                elem.append(Paragraph("Sem dados para os filtros selecionados.", style_nor_pdf))
            elem.append(Spacer(1, 0.6*cm))

            # Seção 2 — Movimentações
            elem.append(Paragraph("2. Movimentações do Período", style_h2_pdf))
            elem.append(Paragraph(f"Entradas: +{fmt_br(total_ent)} cab. | Saídas: -{fmt_br(total_sai)} cab.", style_cap_pdf))
            if not df_saldo_cat.empty:
                data_ms = [['Categoria', 'Saldo Inicial', 'Entradas', 'Saídas', 'Saldo Final']]
                for _, row in df_saldo_cat.iterrows():
                    data_ms.append([row['Categoria'], fmt_br(row['Saldo Inicial']), fmt_br(row['Entradas']),
                                    fmt_br(row['Saídas']), fmt_br(row['Saldo Final'])])
                elem.append(make_pdf_table(data_ms, col_widths=[150, 70, 70, 70, 70], hdr_color=COL_GREEN_PDF))
            if not df_mov.empty:
                elem.append(Spacer(1, 0.3*cm))
                elem.append(Paragraph("Extrato linha a linha:", style_cap_pdf))
                data_ex = [['Data', 'Fazenda', 'Categoria', 'Evento', 'Ent.(+)', 'Saí.(-)']]
                for _, row in df_mov.iterrows():
                    data_ex.append([row['Data'], str(row['Fazenda'])[:18], str(row['Categoria'])[:20],
                                    str(row['Evento'])[:28], fmt_br(row['Entrada (+)']), fmt_br(row['Saída (-)'])])
                elem.append(make_pdf_table(data_ex, col_widths=[42, 62, 72, 90, 40, 40]))
            elem.append(Spacer(1, 0.6*cm))

            # Seção 3 — KPIs
            elem.append(Paragraph("3. KPIs Zootécnicos", style_h2_pdf))
            data_kpi_pdf = [['Indicador', 'Óbitos', 'Pop.', 'Taxa (%)', 'Status', 'Meta Verde']]
            for _, row in df_kpi.iterrows():
                data_kpi_pdf.append([row['Grupo'], row['Óbitos'], row['População'], row['Taxa (%)'],
                                     row['Status'].replace('🟢','[V]').replace('🟡','[A]').replace('🔴','[C]'),
                                     row['Meta Verde']])
            elem.append(make_pdf_table(data_kpi_pdf, col_widths=[115, 40, 45, 48, 60, 62]))
            elem.append(Paragraph("Status: [V] Ideal  [A] Atenção  [C] Crítico", style_cap_pdf))
            elem.append(Spacer(1, 0.6*cm))

            # Seção 4 — Fechamentos
            elem.append(Paragraph("4. Fechamentos Mensais", style_h2_pdf))
            if not df_fech.empty:
                data_fech_pdf = [['Mês', 'Status', 'Registrado em']]
                for _, row in df_fech.iterrows():
                    data_fech_pdf.append([row['Mês'], row['Status'], row['Registrado em']])
                elem.append(make_pdf_table(data_fech_pdf, col_widths=[100, 100, 150]))
            else:
                elem.append(Paragraph("Nenhum fechamento registrado neste período.", style_nor_pdf))

            doc_pdf.build(elem)
            st.download_button(
                "📄 Baixar PDF",
                pdf_buf.getvalue(),
                f"relatorio_ajagro_{d_ini.strftime('%Y%m%d')}_{d_fim.strftime('%Y%m%d')}.pdf",
                mime="application/pdf",
                use_container_width=True
            )

        conn.close()

    # =========================================================
    # TELA: CADASTROS BASE
    # =========================================================
    elif menu == "Cadastros Base":
        st.header("🏢 Cadastro de Fazendas")
        with st.form("add_f"):
            n = st.text_input("Nome da Fazenda")
            c = st.text_input("CNPJ/CPF")
            if st.form_submit_button("Salvar"):
                conn = get_connection()
                conn.execute(text("INSERT INTO fazendas (nome_fazenda, cnpj_cpf) VALUES (:n, :c)"),
                             {"n": n, "c": c})
                conn.commit()
                conn.close()
                st.success("Fazenda cadastrada!")

    # =========================================================
    # TELA: KPIs ZOOTÉCNICOS
    # =========================================================
    elif menu == "📊 KPIs Zootécnicos":
        st.header("📊 KPIs Zootécnicos — Painel de Indicadores")

        conn     = get_connection()
        fazendas = pd.read_sql(text("SELECT id_fazenda, nome_fazenda FROM fazendas"), conn)

        st.sidebar.divider()
        st.sidebar.subheader("Filtros dos KPIs")
        hoje  = date.today()
        d_ini = st.sidebar.date_input("Data Inicial", date(hoje.year, hoje.month, 1), key="kpi_ini")
        d_fim = st.sidebar.date_input("Data Final", hoje, key="kpi_fim")
        faz_opcoes = ["Todas as Fazendas"] + fazendas['nome_fazenda'].tolist()
        faz_sel    = st.sidebar.selectbox("Fazenda", faz_opcoes, key="kpi_faz")

        filtro_faz = ""
        params     = {"d_ini": d_ini, "d_fim": d_fim}
        if faz_sel != "Todas as Fazendas":
            id_faz = int(fazendas[fazendas['nome_fazenda'] == faz_sel]['id_fazenda'].values[0])
            filtro_faz       = "AND id_fazenda = :id_faz"
            params["id_faz"] = id_faz

        def calc_kpi_grupo(conn, categorias, params, filtro_faz):
            cats_sql = ", ".join([f"'{c}'" for c in categorias])
            q_obitos = text(f"""
                SELECT COALESCE(SUM(quantidade), 0) FROM lanc_estoque
                WHERE evento = 'Saída/Mortes' AND categoria IN ({cats_sql})
                  AND data_movimento BETWEEN :d_ini AND :d_fim {filtro_faz}
            """)
            obitos = conn.execute(q_obitos, params).scalar() or 0
            q_saldo_ini = text(f"""
                SELECT COALESCE(SUM(
                    CASE WHEN evento LIKE 'Entrada%%' OR evento LIKE 'Transferências/De%%'
                         THEN quantidade ELSE -quantidade END), 0)
                FROM lanc_estoque
                WHERE categoria IN ({cats_sql}) AND data_movimento < :d_ini {filtro_faz}
            """)
            saldo_ini = conn.execute(q_saldo_ini, params).scalar() or 0
            q_entradas = text(f"""
                SELECT COALESCE(SUM(quantidade), 0) FROM lanc_estoque
                WHERE (evento LIKE 'Entrada%%' OR evento LIKE 'Transferências/De%%')
                  AND categoria IN ({cats_sql})
                  AND data_movimento BETWEEN :d_ini AND :d_fim {filtro_faz}
            """)
            entradas  = conn.execute(q_entradas, params).scalar() or 0
            populacao = saldo_ini + entradas
            taxa      = (obitos / populacao * 100) if populacao > 0 else 0.0
            return int(obitos), int(populacao), round(taxa, 2)

        def semaforo(taxa, verde_max, amarelo_max):
            if taxa < verde_max:
                return "🟢", "#27ae60", "IDEAL"
            elif taxa <= amarelo_max:
                return "🟡", "#f39c12", "ATENÇÃO"
            else:
                return "🔴", "#e74c3c", "CRÍTICO"

        # Carrega parâmetros do banco (ou fallback)
        _p = get_parametros()

        grupos_mortalidade = {
            "Bezerros (0–60 dias)": {
                "categorias": ["Mamando - Machos", "Mamando - Fêmeas"],
                "verde_max":   _p["mort_bezerros"]["verde_max"],
                "amarelo_max": _p["mort_bezerros"]["amarelo_max"],
                "descricao":   _p["mort_bezerros"]["descricao"],
            },
            "Recria (2–12 meses)": {
                "categorias": ["Novilhas até 1 ano"],
                "verde_max":   _p["mort_recria"]["verde_max"],
                "amarelo_max": _p["mort_recria"]["amarelo_max"],
                "descricao":   _p["mort_recria"]["descricao"],
            },
            "Novilhas (>12 meses)": {
                "categorias": ["Novilhas de 1 a 2 anos", "Novilhas Prenhas"],
                "verde_max":   _p["mort_novilhas"]["verde_max"],
                "amarelo_max": _p["mort_novilhas"]["amarelo_max"],
                "descricao":   _p["mort_novilhas"]["descricao"],
            },
            "Vacas Adultas": {
                "categorias": ["Vacas Lactantes", "Vacas Secas",
                               "Vacas a refugar", "Vacas refugadas"],
                "verde_max":   _p["mort_vacas"]["verde_max"],
                "amarelo_max": _p["mort_vacas"]["amarelo_max"],
                "descricao":   _p["mort_vacas"]["descricao"],
            },
        }

        st.subheader(f"🚦 Taxas de Mortalidade — {d_ini.strftime('%d/%m/%Y')} a {d_fim.strftime('%d/%m/%Y')}")
        st.caption(f"Fazenda: **{faz_sel}** | Denominador: Saldo Inicial + Entradas do período")

        col_pares  = [st.columns(2), st.columns(2)]
        kpi_results = []
        for idx, (nome, cfg) in enumerate(grupos_mortalidade.items()):
            obitos, pop, taxa = calc_kpi_grupo(conn, cfg["categorias"], params, filtro_faz)
            emoji, cor, label = semaforo(taxa, cfg["verde_max"], cfg["amarelo_max"])
            kpi_results.append({
                "nome": nome, "obitos": obitos, "pop": pop,
                "taxa": taxa, "emoji": emoji, "cor": cor, "label": label,
                "descricao": cfg["descricao"],
                "verde_max": cfg["verde_max"], "amarelo_max": cfg["amarelo_max"]
            })
            col = col_pares[idx // 2][idx % 2]
            with col:
                st.markdown(f"""
                <div style="border:2px solid {cor}; border-radius:12px; padding:16px;
                            background:{'#f9fbe7' if cor=='#f39c12' else ('#fdecea' if cor=='#e74c3c' else '#f0faf4')};">
                    <div style="font-size:1.1rem; font-weight:700; color:#2c3e50;">{emoji} {nome}</div>
                    <div style="font-size:2.4rem; font-weight:900; color:{cor};">{fmt_br(taxa, 2)}%</div>
                    <div style="font-size:0.85rem; color:#555;">
                        {fmt_br(obitos)} óbitos / {fmt_br(pop)} animais no período
                    </div>
                    <div style="margin-top:6px;">
                        <span style="background:{cor}; color:white; padding:2px 10px;
                                     border-radius:20px; font-size:0.8rem; font-weight:700;">
                            {label}
                        </span>
                    </div>
                    <div style="font-size:0.78rem; color:#777; margin-top:8px;">{cfg['descricao']}</div>
                    <div style="font-size:0.75rem; color:#aaa; margin-top:4px;">
                        Meta: &lt;{fmt_br(cfg['verde_max'], 1)}% ✅ | Atenção: até {fmt_br(cfg['amarelo_max'], 1)}% ⚠️
                    </div>
                </div>
                """, unsafe_allow_html=True)

        st.divider()
        st.subheader("🐄 Composição e Eficiência do Rebanho")

        q_saldo = text(f"""
            SELECT categoria,
                   SUM(CASE WHEN evento LIKE 'Entrada%%' OR evento LIKE 'Transferências/De%%'
                            THEN quantidade ELSE -quantidade END) as saldo
            FROM lanc_estoque
            WHERE data_movimento <= :d_fim {filtro_faz}
            GROUP BY categoria
        """)
        df_saldo   = pd.read_sql(q_saldo, conn, params={"d_fim": d_fim, **({"id_faz": params["id_faz"]} if "id_faz" in params else {})})
        saldo_dict = dict(zip(df_saldo['categoria'], df_saldo['saldo'].clip(lower=0)))

        lactantes     = saldo_dict.get("Vacas Lactantes", 0)
        secas         = saldo_dict.get("Vacas Secas", 0)
        total_rebanho = sum(v for v in saldo_dict.values() if v > 0)
        total_vacas   = lactantes + secas

        pct_lact_vacas   = (lactantes / total_vacas   * 100) if total_vacas   > 0 else 0.0
        pct_lact_rebanho = (lactantes / total_rebanho * 100) if total_rebanho > 0 else 0.0

        def semaforo_comp(valor, verde_min, amarelo_min):
            if valor >= verde_min:
                return "🟢", "#27ae60", "IDEAL"
            elif valor >= amarelo_min:
                return "🟡", "#f39c12", "ATENÇÃO"
            else:
                return "🔴", "#e74c3c", "CRÍTICO"

        # Limiares agora vêm do banco via get_parametros()
        em1, cor1, lb1 = semaforo_comp(
            pct_lact_vacas,
            _p["comp_lact_vacas"]["verde_max"],
            _p["comp_lact_vacas"]["amarelo_max"]
        )
        em2, cor2, lb2 = semaforo_comp(
            pct_lact_rebanho,
            _p["comp_lact_rebanho"]["verde_max"],
            _p["comp_lact_rebanho"]["amarelo_max"]
        )

        # Metas dinâmicas para exibição nos cards
        meta_lv_v = f"≥ {fmt_br(_p['comp_lact_vacas']['verde_max'], 1)}%"
        meta_lv_a = f"{fmt_br(_p['comp_lact_vacas']['amarelo_max'], 1)}% a {fmt_br(_p['comp_lact_vacas']['verde_max'] - 1, 1)}%"
        meta_lr_v = f"≥ {fmt_br(_p['comp_lact_rebanho']['verde_max'], 1)}%"
        meta_lr_a = f"{fmt_br(_p['comp_lact_rebanho']['amarelo_max'], 1)}% a {fmt_br(_p['comp_lact_rebanho']['verde_max'] - 1, 1)}%"

        c1, c2 = st.columns(2)
        for col, emoji, cor, label, titulo, valor, meta_v, meta_a, descricao in [
            (c1, em1, cor1, lb1,
             "% Lactantes / Total de Vacas", pct_lact_vacas,
             meta_lv_v, meta_lv_a,
             f"Lactantes: {fmt_br(lactantes)} | Secas: {fmt_br(secas)} | Total Vacas: {fmt_br(total_vacas)}"),
            (c2, em2, cor2, lb2,
             "% Lactantes / Rebanho Total", pct_lact_rebanho,
             meta_lr_v, meta_lr_a,
             f"Lactantes: {fmt_br(lactantes)} | Rebanho Total: {fmt_br(total_rebanho)} animais"),
        ]:
            with col:
                st.markdown(f"""
                <div style="border:2px solid {cor}; border-radius:12px; padding:16px;
                            background:{'#f9fbe7' if cor=='#f39c12' else ('#fdecea' if cor=='#e74c3c' else '#f0faf4')};">
                    <div style="font-size:1.1rem; font-weight:700; color:#2c3e50;">{emoji} {titulo}</div>
                    <div style="font-size:2.4rem; font-weight:900; color:{cor};">{fmt_br(valor, 1)}%</div>
                    <div style="font-size:0.85rem; color:#555;">{descricao}</div>
                    <div style="margin-top:6px;">
                        <span style="background:{cor}; color:white; padding:2px 10px;
                                     border-radius:20px; font-size:0.8rem; font-weight:700;">
                            {label}
                        </span>
                    </div>
                    <div style="font-size:0.75rem; color:#aaa; margin-top:8px;">
                        Meta Ideal: {meta_v} ✅ | Atenção: {meta_a} ⚠️
                    </div>
                </div>
                """, unsafe_allow_html=True)

        st.divider()
        st.subheader("📋 Tabela Resumo — Todos os Indicadores")
        rows = []
        for r in kpi_results:
            rows.append({
                "Indicador":      r["nome"],
                "Óbitos":         fmt_br(r["obitos"]),
                "População":      fmt_br(r["pop"]),
                "Taxa (%)":       fmt_br(r["taxa"], 2),
                "Status":         f"{r['emoji']} {r['label']}",
                "Meta Verde":     f"< {fmt_br(r['verde_max'], 1)}%",
                "Limite Crítico": f"> {fmt_br(r['amarelo_max'], 1)}%",
            })
        rows.append({
            "Indicador":      "% Lactantes / Total Vacas",
            "Óbitos":         "—",
            "População":      fmt_br(total_vacas),
            "Taxa (%)":       fmt_br(pct_lact_vacas, 2),
            "Status":         f"{em1} {lb1}",
            "Meta Verde":     meta_lv_v,
            "Limite Crítico": f"< {fmt_br(_p['comp_lact_vacas']['amarelo_max'], 1)}%",
        })
        rows.append({
            "Indicador":      "% Lactantes / Rebanho Total",
            "Óbitos":         "—",
            "População":      fmt_br(total_rebanho),
            "Taxa (%)":       fmt_br(pct_lact_rebanho, 2),
            "Status":         f"{em2} {lb2}",
            "Meta Verde":     meta_lr_v,
            "Limite Crítico": f"< {fmt_br(_p['comp_lact_rebanho']['amarelo_max'], 1)}%",
        })
        df_resumo = pd.DataFrame(rows)
        st.dataframe(df_resumo, use_container_width=True, hide_index=True)

        buf_kpi = io.BytesIO()
        df_resumo.to_excel(buf_kpi, index=False)
        st.download_button("📥 Exportar KPIs (Excel)", buf_kpi.getvalue(),
                           "kpis_zootecnicos.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

        conn.close()

    # =========================================================
    # TELA: FECHAMENTO MENSAL
    # =========================================================
    elif menu == "Fechamento Mensal":
        st.header("🔒 Fechamento Contábil")
        mes = st.date_input("Mês para Fechar", date.today().replace(day=1))
        if st.button("Executar Fechamento"):
            conn = get_connection()
            mes_normalizado = mes.replace(day=1)
            conn.execute(text("""
                INSERT INTO fechamentos_mensais (ano_mes, status) VALUES (:m, 'Fechado')
                ON CONFLICT (ano_mes) DO UPDATE SET status = 'Fechado'
            """), {"m": mes_normalizado})
            conn.commit()
            conn.close()
            st.success(f"Mês {mes_normalizado.strftime('%m/%Y')} fechado!")

    # =========================================================
    # TELA: PARÂMETROS AJAGRO (novo v3.3)
    # =========================================================
    elif menu == "⚙️ Parâmetros AJAGRO":
        st.header("⚙️ Parâmetros AJAGRO — Metas dos KPIs Zootécnicos")
        st.caption("Ajuste os limiares de semáforo sem precisar alterar o código. As mudanças entram em vigor no próximo carregamento dos KPIs (cache de 5 min ou imediatamente ao salvar).")

        params_atuais = get_parametros()
        conn = get_connection()

        st.subheader("🐄 Taxas de Mortalidade")
        st.markdown("Para cada grupo, defina a taxa (%) que separa os status **Verde / Amarelo / Vermelho**.")

        GRUPOS_LABELS = {
            "mort_bezerros":  "Bezerros (0–60 dias)",
            "mort_recria":    "Recria (2–12 meses)",
            "mort_novilhas":  "Novilhas (>12 meses)",
            "mort_vacas":     "Vacas Adultas",
        }

        with st.form("form_mort"):
            novos_mort = {}
            for chave, label in GRUPOS_LABELS.items():
                atual = params_atuais.get(chave, {"verde_max": 3.0, "amarelo_max": 5.0, "descricao": ""})
                st.markdown(f"**{label}**")
                c1, c2, c3 = st.columns([2, 1, 1])
                with c1:
                    desc = st.text_input("Descrição", value=atual["descricao"], key=f"desc_{chave}")
                with c2:
                    verde = st.number_input(
                        "🟢 Meta verde (< %)", min_value=0.0, max_value=100.0,
                        value=float(atual["verde_max"]), step=0.5, format="%.1f",
                        key=f"verde_{chave}"
                    )
                with c3:
                    amarelo = st.number_input(
                        "🟡 Limite amarelo (< %)", min_value=0.0, max_value=100.0,
                        value=float(atual["amarelo_max"]), step=0.5, format="%.1f",
                        key=f"amarelo_{chave}"
                    )
                if verde >= amarelo:
                    st.error(f"⚠️ Meta verde ({verde}%) deve ser menor que o limite amarelo ({amarelo}%).")
                novos_mort[chave] = {"verde_max": verde, "amarelo_max": amarelo, "descricao": desc}
                st.divider()

            if st.form_submit_button("💾 Salvar Mortalidade", type="primary"):
                erro = any(v["verde_max"] >= v["amarelo_max"] for v in novos_mort.values())
                if erro:
                    st.error("Corrija os valores antes de salvar: meta verde deve ser menor que limite amarelo.")
                else:
                    for chave, vals in novos_mort.items():
                        conn.execute(text("""
                            INSERT INTO parametros_ajagro (chave, verde_max, amarelo_max, descricao)
                            VALUES (:chave, :v, :a, :d)
                            ON CONFLICT (chave) DO UPDATE
                                SET verde_max   = EXCLUDED.verde_max,
                                    amarelo_max = EXCLUDED.amarelo_max,
                                    descricao   = EXCLUDED.descricao
                        """), {"chave": chave, "v": vals["verde_max"],
                               "a": vals["amarelo_max"], "d": vals["descricao"]})
                    conn.commit()
                    get_parametros.clear()
                    st.success("✅ Parâmetros de mortalidade atualizados!")
                    st.rerun()

        st.divider()
        st.subheader("🐄 Composição do Rebanho")
        st.markdown("Para estes indicadores, **verde** é o valor mínimo para status verde (quanto maior, melhor).")

        COMP_LABELS = {
            "comp_lact_vacas":   "% Lactantes / Total de Vacas",
            "comp_lact_rebanho": "% Lactantes / Rebanho Total",
        }

        with st.form("form_comp"):
            novos_comp = {}
            for chave, label in COMP_LABELS.items():
                atual = params_atuais.get(chave, {"verde_max": 83.0, "amarelo_max": 75.0, "descricao": ""})
                st.markdown(f"**{label}**")
                c1, c2, c3 = st.columns([2, 1, 1])
                with c1:
                    desc = st.text_input("Descrição", value=atual["descricao"], key=f"desc_{chave}")
                with c2:
                    verde = st.number_input(
                        "🟢 Meta verde (≥ %)", min_value=0.0, max_value=100.0,
                        value=float(atual["verde_max"]), step=0.5, format="%.1f",
                        key=f"verde_{chave}"
                    )
                with c3:
                    amarelo = st.number_input(
                        "🟡 Limite amarelo (≥ %)", min_value=0.0, max_value=100.0,
                        value=float(atual["amarelo_max"]), step=0.5, format="%.1f",
                        key=f"amarelo_{chave}"
                    )
                if amarelo >= verde:
                    st.error(f"⚠️ Limite amarelo ({amarelo}%) deve ser menor que meta verde ({verde}%).")
                novos_comp[chave] = {"verde_max": verde, "amarelo_max": amarelo, "descricao": desc}
                st.divider()

            if st.form_submit_button("💾 Salvar Composição", type="primary"):
                erro = any(v["amarelo_max"] >= v["verde_max"] for v in novos_comp.values())
                if erro:
                    st.error("Corrija os valores antes de salvar.")
                else:
                    for chave, vals in novos_comp.items():
                        conn.execute(text("""
                            INSERT INTO parametros_ajagro (chave, verde_max, amarelo_max, descricao)
                            VALUES (:chave, :v, :a, :d)
                            ON CONFLICT (chave) DO UPDATE
                                SET verde_max   = EXCLUDED.verde_max,
                                    amarelo_max = EXCLUDED.amarelo_max,
                                    descricao   = EXCLUDED.descricao
                        """), {"chave": chave, "v": vals["verde_max"],
                               "a": vals["amarelo_max"], "d": vals["descricao"]})
                    conn.commit()
                    get_parametros.clear()
                    st.success("✅ Parâmetros de composição atualizados!")
                    st.rerun()

        conn.close()

    # =========================================================
    # TELA: AJUSTE DE PREÇOS
    # =========================================================
    elif menu == "⚙️ Ajuste de Preços":
        st.header("⚙️ Ajuste de Preços")
        conn = get_connection()
        p_df = pd.read_sql(text("SELECT * FROM precos_gestao"), conn)
        with st.form("p"):
            dict_n = {}
            for cat in CATEGORIAS_LISTA:
                val_atual = p_df[p_df['categoria'] == cat]['valor'].values[0] \
                    if cat in p_df['categoria'].values else 0.0
                dict_n[cat] = st.number_input(f"{cat} (R$)", value=float(val_atual))
            if st.form_submit_button("Salvar Preços"):
                for c, v in dict_n.items():
                    conn.execute(text("""
                        INSERT INTO precos_gestao (categoria, valor) VALUES (:c, :v)
                        ON CONFLICT (categoria) DO UPDATE SET valor = EXCLUDED.valor
                    """), {"c": c, "v": v})
                conn.commit()
                st.success("Preços atualizados!")
        conn.close()

        