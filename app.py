import streamlit as st
import pandas as pd
from sqlalchemy import create_engine, text
from datetime import date
import io
import plotly.express as px
import plotly.graph_objects as go
from reportlab.lib.pagesizes import letter, A4
from reportlab.pdfgen import canvas
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet

# --- CONFIGURAÇÃO DA PÁGINA ---
st.set_page_config(page_title="AJAGRO - Gestão Pecuária 3.2", layout="wide")

# --- CONEXÃO ---
def get_connection():
    db_url = st.secrets["DB_CONN_STRING"].replace("postgres://", "postgresql+psycopg2://", 1)
    return create_engine(db_url).connect()

# --- CATEGORIAS OFICIAIS (ITEM 6) ---
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

# --- FUNÇÕES DE APOIO ---
def get_saldo_atual(conn, fazenda_id, categoria):
    query = text("""
        SELECT SUM(CASE WHEN evento LIKE 'Entrada%%' OR evento LIKE 'Transferências/De%%'
                        THEN quantidade ELSE -quantidade END)
        FROM lanc_estoque WHERE id_fazenda = :f AND categoria = :c
    """)
    res = conn.execute(query, {"f": fazenda_id, "c": categoria}).scalar()
    return res if res else 0


# ============================================================
# CORREÇÃO v3.2 — Bug de Fuso Horário em is_mes_fechado
# ============================================================
# PROBLEMA ANTERIOR:
#   TO_CHAR(ano_mes, 'YYYY-MM-DD') = :d
#   A coluna ano_mes é TIMESTAMPTZ no Supabase.
#   Dependendo do timezone do servidor, '2026-03-01 00:00:00+00'
#   pode ser convertida para '2026-02-28' via TO_CHAR, fazendo a
#   comparação falhar silenciosamente e liberando lançamentos
#   retroativos em meses já fechados.
#
# SOLUÇÃO:
#   Forçar a normalização para UTC com AT TIME ZONE 'UTC' antes
#   de converter para DATE. Comparar DATE vs DATE, sem strings.
# ============================================================
def is_mes_fechado(conn, data_mov):
    # Normaliza para o primeiro dia do mês como objeto date Python
    primeiro_dia = data_mov.replace(day=1)
    query = text("""
        SELECT status FROM fechamentos_mensais
        WHERE DATE(ano_mes AT TIME ZONE 'UTC') = :d
          AND status = 'Fechado'
    """)
    try:
        res = conn.execute(query, {"d": primeiro_dia}).fetchone()
        return res is not None
    except:
        return False


# --- LOGIN ---
if "autenticado" not in st.session_state:
    st.session_state["autenticado"] = False

if not st.session_state["autenticado"]:
    st.markdown("<h1 style='text-align: center;'>🔐 AJAGRO 3.2 - Acesso</h1>",
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
        "📊 KPIs Zootécnicos",           # NOVO em v3.2
        "Lançamento de Eventos",
        "Cadastros Base",
        "Fechamento Mensal",
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
                data_mov = st.date_input("Data do Evento", date.today())
                faz_sel = st.selectbox("Fazenda", fazendas['nome_fazenda'])
                evento_sel = st.selectbox("Tipo de Evento", EVENTOS_ENTRADA + EVENTOS_SAIDA)
            with col2:
                if evento_sel == "Entrada/Nascimento":
                    cat_opcoes = ["Mamando - Machos", "Mamando - Fêmeas"]
                else:
                    cat_opcoes = CATEGORIAS_LISTA
                cat_sel = st.selectbox("Categoria", cat_opcoes)
                qtd = st.number_input("Quantidade de Cabeças", min_value=1, step=1)
                obs = st.text_area("Observações (Obrigatório para Nascimentos e Saídas)")

            fluxo_transferencia = {
                "Mamando - Machos":      "Machos",
                "Mamando - Fêmeas":      "Novilhas até 1 ano",
                "Novilhas até 1 ano":    "Novilhas de 1 a 2 anos",
                "Novilhas de 1 a 2 anos":"Novilhas Prenhas",
                "Novilhas Prenhas":      "Vacas Lactantes",
                "Vacas Lactantes":       "Vacas Secas"
            }

            if st.button("Confirmar Lançamento"):
                id_f = int(fazendas[fazendas['nome_fazenda'] == faz_sel]['id_fazenda'].values[0])

                # ------------------------------------------------
                # VALIDAÇÃO: Mês Fechado (usa função corrigida v3.2)
                # ------------------------------------------------
                if is_mes_fechado(conn, data_mov):
                    st.error(f"⛔ O mês {data_mov.strftime('%m/%Y')} já foi FECHADO e não "
                             f"permite novos lançamentos retroativos.")
                elif (evento_sel == "Entrada/Nascimento" or evento_sel in EVENTOS_SAIDA) and not obs:
                    st.error("Para este evento, o campo observação é obrigatório.")
                else:
                    pode_gravar = True
                    if evento_sel in EVENTOS_SAIDA or evento_sel == "Transferências/Para Outras Categorias":
                        saldo_atual = get_saldo_atual(conn, id_f, cat_sel)
                        if saldo_atual < qtd:
                            st.error(f"Saldo insuficiente! Estoque de {cat_sel}: {saldo_atual} cab.")
                            pode_gravar = False

                    if pode_gravar:
                        try:
                            if evento_sel == "Transferências/Para Outras Categorias":
                                cat_destino = fluxo_transferencia.get(cat_sel)
                                if not cat_destino:
                                    st.error(f"A categoria '{cat_sel}' não possui destino definido no fluxo.")
                                else:
                                    sql_sai = text("INSERT INTO lanc_estoque (data_movimento, id_fazenda, quantidade, evento, categoria, observacao) VALUES (:d, :f, :q, :e, :c, :o)")
                                    conn.execute(sql_sai, {"d": data_mov, "f": id_f, "q": qtd,
                                                           "e": "Transferências/Para Outras Categorias",
                                                           "c": cat_sel,
                                                           "o": f"Saída p/ {cat_destino}. Obs: {obs}"})
                                    sql_ent = text("INSERT INTO lanc_estoque (data_movimento, id_fazenda, quantidade, evento, categoria, observacao) VALUES (:d, :f, :q, :e, :c, :o)")
                                    conn.execute(sql_ent, {"d": data_mov, "f": id_f, "q": qtd,
                                                           "e": "Transferências/De Outras Categorias",
                                                           "c": cat_destino,
                                                           "o": f"Entrada vinda de {cat_sel}. Obs: {obs}"})
                                    conn.commit()
                                    st.success(f"Transferência concluída! {qtd} cab. saíram de {cat_sel} e entraram em {cat_destino}.")
                                    st.balloons()
                            else:
                                sql = text("INSERT INTO lanc_estoque (data_movimento, id_fazenda, quantidade, evento, categoria, observacao) VALUES (:d, :f, :q, :e, :c, :o)")
                                conn.execute(sql, {"d": data_mov, "f": id_f, "q": qtd,
                                                   "e": evento_sel, "c": cat_sel, "o": obs})
                                conn.commit()
                                st.success(f"Sucesso! {qtd} '{cat_sel}' registrado como '{evento_sel}'.")
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
        hoje = date.today()
        d_ini = st.sidebar.date_input("Data Inicial", date(hoje.year, hoje.month, 1))
        d_fim = st.sidebar.date_input("Data Final", hoje)

        # --------------------------------------------------------
        # IMPORTANTE: O saldo do balanço é ACUMULADO HISTÓRICO
        # até d_fim (ignora d_ini intencionalmente).
        # d_ini é usado apenas no histórico de lançamentos abaixo.
        # O Módulo de Auditoria (menu separado) mostra o filtro
        # por período com saldo inicial + movimentações.
        # --------------------------------------------------------
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
            df_p = pd.read_sql(text("SELECT * FROM precos_gestao"), conn)
            dict_p = dict(zip(df_p['categoria'], df_p['valor']))
            df_bal['Preço Unit.'] = df_bal['Categoria'].map(dict_p).fillna(0)
            df_bal['Total R$'] = df_bal['Estoque'] * df_bal['Preço Unit.']
            df_bal = df_bal[df_bal['Estoque'] > 0]  # Remove categorias zeradas

            total_r = df_bal['Total R$'].sum()
            total_c = df_bal['Estoque'].sum()
            m1, m2, m3 = st.columns(3)
            m1.metric("Estoque Total", f"{int(total_c)} cab.")
            m2.metric("Valorização Total", f"R$ {total_r:,.2f}")
            m3.metric("Média por Animal", f"R$ {(total_r / total_c if total_c > 0 else 0):,.2f}")

            st.divider()
            col_g, col_t = st.columns([1, 1.2])
            with col_g:
                st.subheader("Distribuição do Patrimônio")
                fig = px.pie(df_bal[df_bal['Total R$'] > 0], values='Total R$',
                             names='Categoria', hole=0.4)
                st.plotly_chart(fig, use_container_width=True)
            with col_t:
                st.subheader("Tabela de Participação")
                st.dataframe(
                    df_bal.style.format({'Preço Unit.': 'R$ {:.2f}', 'Total R$': 'R$ {:.2f}'}),
                    use_container_width=True, hide_index=True
                )
                c_ex1, c_ex2 = st.columns(2)
                with c_ex1:
                    buf = io.BytesIO()
                    df_bal.to_excel(buf, index=False)
                    st.download_button("📥 Excel", buf.getvalue(), "balanco.xlsx")
                with c_ex2:
                    pdf_buf = io.BytesIO()
                    canv = canvas.Canvas(pdf_buf, pagesize=letter)
                    canv.drawString(50, 750, f"AJAGRO - BALANÇO EM {d_fim.strftime('%d/%m/%Y')}")
                    y = 720
                    for _, row in df_bal.iterrows():
                        canv.drawString(50, y, f"{row['Categoria']}: {row['Estoque']} cab. - R$ {row['Total R$']:,.2f}")
                        y -= 20
                    canv.save()
                    st.download_button("📥 PDF", pdf_buf.getvalue(), "balanco.pdf")

            st.divider()
            st.subheader("📑 Últimos Lançamentos")
            df_hist = pd.read_sql(text("""
                SELECT TO_CHAR(data_movimento,'DD/MM/YYYY') as Data,
                       evento as Evento, categoria as Categoria,
                       quantidade as Quantidade
                FROM lanc_estoque ORDER BY id_lancamento DESC LIMIT 20
            """), conn)
            st.table(df_hist)
        else:
            st.warning("Nenhum lançamento encontrado até a data selecionada.")

        conn.close()

    # =========================================================
    # NOVA TELA v3.2: MÓDULO DE AUDITORIA
    # =========================================================
    # LÓGICA DE SALDO:
    #   Saldo Inicial  = acumulado de TODOS os lançamentos ANTES de d_ini
    #   Movimentações  = lançamentos DENTRO do período [d_ini, d_fim]
    #   Saldo Final    = Saldo Inicial + Entradas do período - Saídas do período
    #
    # Isso separa claramente "o que eu tinha antes" de "o que movimentei agora",
    # corrigindo a confusão entre Saldo Acumulado Histórico e Filtro de Período.
    # =========================================================
    elif menu == "📋 Módulo de Auditoria":
        st.header("📋 Módulo de Auditoria — Extrato de Movimentações")

        conn = get_connection()
        fazendas = pd.read_sql(text("SELECT id_fazenda, nome_fazenda FROM fazendas"), conn)

        st.sidebar.divider()
        st.sidebar.subheader("Filtros da Auditoria")
        hoje = date.today()
        d_ini = st.sidebar.date_input("Data Inicial", date(hoje.year, hoje.month, 1), key="aud_ini")
        d_fim = st.sidebar.date_input("Data Final", hoje, key="aud_fim")

        faz_opcoes = ["Todas as Fazendas"] + fazendas['nome_fazenda'].tolist()
        faz_sel = st.sidebar.selectbox("Fazenda", faz_opcoes, key="aud_faz")
        cat_opcoes = ["Todas as Categorias"] + CATEGORIAS_LISTA
        cat_sel = st.sidebar.selectbox("Categoria", cat_opcoes, key="aud_cat")

        # Monta filtros dinâmicos
        filtro_faz_sql = ""
        filtro_cat_sql = ""
        params_base = {"d_ini": d_ini, "d_fim": d_fim}

        if faz_sel != "Todas as Fazendas":
            id_faz = int(fazendas[fazendas['nome_fazenda'] == faz_sel]['id_fazenda'].values[0])
            filtro_faz_sql = "AND id_fazenda = :id_faz"
            params_base["id_faz"] = id_faz

        if cat_sel != "Todas as Categorias":
            filtro_cat_sql = "AND categoria = :cat"
            params_base["cat"] = cat_sel

        # --------------------------------------------------------
        # SEÇÃO 1: SALDO INICIAL (tudo antes de d_ini)
        # --------------------------------------------------------
        q_saldo_ini = text(f"""
            SELECT categoria,
                   SUM(CASE WHEN evento LIKE 'Entrada%%' OR evento LIKE 'Transferências/De%%'
                            THEN quantidade ELSE -quantidade END) as saldo_inicial
            FROM lanc_estoque
            WHERE data_movimento < :d_ini
            {filtro_faz_sql}
            {filtro_cat_sql}
            GROUP BY categoria
        """)
        df_saldo_ini = pd.read_sql(q_saldo_ini, conn, params=params_base)

        # --------------------------------------------------------
        # SEÇÃO 2: MOVIMENTAÇÕES DO PERÍODO [d_ini, d_fim]
        # --------------------------------------------------------
        q_mov = text(f"""
            SELECT categoria,
                   SUM(CASE WHEN evento LIKE 'Entrada%%' OR evento LIKE 'Transferências/De%%'
                            THEN quantidade ELSE 0 END) as entradas,
                   SUM(CASE WHEN evento LIKE 'Saída%%' OR evento LIKE 'Transferências/Para%%'
                            THEN quantidade ELSE 0 END) as saidas
            FROM lanc_estoque
            WHERE data_movimento BETWEEN :d_ini AND :d_fim
            {filtro_faz_sql}
            {filtro_cat_sql}
            GROUP BY categoria
        """)
        df_mov = pd.read_sql(q_mov, conn, params=params_base)

        # --------------------------------------------------------
        # SEÇÃO 3: MONTA TABELA DE SALDO CONSOLIDADO
        # --------------------------------------------------------
        todas_cats = pd.DataFrame({'categoria': CATEGORIAS_LISTA if cat_sel == "Todas as Categorias" else [cat_sel]})
        df_cons = todas_cats.merge(df_saldo_ini, on='categoria', how='left')
        df_cons = df_cons.merge(df_mov, on='categoria', how='left')
        df_cons = df_cons.fillna(0)
        df_cons['saldo_final'] = df_cons['saldo_inicial'] + df_cons['entradas'] - df_cons['saidas']

        # Remove categorias sem nenhuma movimentação histórica
        df_cons = df_cons[(df_cons['saldo_inicial'] != 0) |
                          (df_cons['entradas'] != 0) |
                          (df_cons['saidas'] != 0)]

        # --------------------------------------------------------
        # SEÇÃO 4: KPIs DO PERÍODO
        # --------------------------------------------------------
        total_ini = int(df_cons['saldo_inicial'].sum())
        total_ent = int(df_cons['entradas'].sum())
        total_sai = int(df_cons['saidas'].sum())
        total_fim = int(df_cons['saldo_final'].sum())

        st.subheader(f"Resumo do Período: {d_ini.strftime('%d/%m/%Y')} → {d_fim.strftime('%d/%m/%Y')}")
        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Saldo Inicial", f"{total_ini} cab.", help="Acumulado histórico antes do período")
        k2.metric("Entradas no Período", f"+ {total_ent} cab.")
        k3.metric("Saídas no Período",   f"- {total_sai} cab.")
        k4.metric("Saldo Final",         f"{total_fim} cab.",
                  delta=f"{total_fim - total_ini:+d} cab.")

        st.divider()

        # --------------------------------------------------------
        # SEÇÃO 5: TABELA DE SALDO POR CATEGORIA
        # --------------------------------------------------------
        st.subheader("📊 Saldo por Categoria")
        df_display = df_cons.rename(columns={
            'categoria':     'Categoria',
            'saldo_inicial': 'Saldo Inicial',
            'entradas':      'Entradas',
            'saidas':        'Saídas',
            'saldo_final':   'Saldo Final'
        })

        def highlight_saldo(val):
            if val < 0:
                return 'color: red; font-weight: bold'
            return ''

        st.dataframe(
            df_display.style
                .map(highlight_saldo, subset=['Saldo Final'])
                .format({'Saldo Inicial': '{:.0f}', 'Entradas': '{:.0f}',
                         'Saídas': '{:.0f}', 'Saldo Final': '{:.0f}'}),
            use_container_width=True,
            hide_index=True
        )

        st.divider()

        # --------------------------------------------------------
        # SEÇÃO 6: GRÁFICO — Entradas vs Saídas por Categoria
        # --------------------------------------------------------
        st.subheader("📈 Movimentações do Período por Categoria")
        df_graf = df_cons[df_cons['entradas'] + df_cons['saidas'] > 0]
        if not df_graf.empty:
            fig_bar = go.Figure()
            fig_bar.add_trace(go.Bar(name='Entradas', x=df_graf['categoria'],
                                     y=df_graf['entradas'], marker_color='#2ecc71'))
            fig_bar.add_trace(go.Bar(name='Saídas', x=df_graf['categoria'],
                                     y=df_graf['saidas'], marker_color='#e74c3c'))
            fig_bar.update_layout(barmode='group', xaxis_tickangle=-30,
                                  legend=dict(orientation="h"),
                                  margin=dict(b=80))
            st.plotly_chart(fig_bar, use_container_width=True)
        else:
            st.info("Nenhuma movimentação registrada neste período para os filtros selecionados.")

        st.divider()

        # --------------------------------------------------------
        # SEÇÃO 7: EXTRATO DETALHADO (linha a linha)
        # --------------------------------------------------------
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
            {filtro_faz_sql}
            {filtro_cat_sql}
            ORDER BY l.data_movimento ASC, l.id_lancamento ASC
        """)
        df_extrato = pd.read_sql(q_extrato, conn, params=params_base)

        if not df_extrato.empty:
            st.dataframe(df_extrato, use_container_width=True, hide_index=True)

            # ---- EXPORTAÇÕES ----
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
                # PDF de Auditoria com ReportLab
                pdf_buf = io.BytesIO()
                doc = SimpleDocTemplate(pdf_buf, pagesize=A4,
                                        leftMargin=30, rightMargin=30,
                                        topMargin=40, bottomMargin=30)
                styles = getSampleStyleSheet()
                elements = []

                elements.append(Paragraph(
                    f"AJAGRO — Extrato de Auditoria",
                    styles['Title']
                ))
                elements.append(Paragraph(
                    f"Período: {d_ini.strftime('%d/%m/%Y')} a {d_fim.strftime('%d/%m/%Y')} | "
                    f"Fazenda: {faz_sel} | Categoria: {cat_sel}",
                    styles['Normal']
                ))
                elements.append(Spacer(1, 12))

                # Tabela de saldo
                elements.append(Paragraph("Saldo por Categoria", styles['Heading2']))
                data_tab = [['Categoria', 'Saldo Ini.', 'Entradas', 'Saídas', 'Saldo Final']]
                for _, row in df_cons.iterrows():
                    data_tab.append([
                        row['categoria'],
                        str(int(row['saldo_inicial'])),
                        str(int(row['entradas'])),
                        str(int(row['saidas'])),
                        str(int(row['saldo_final']))
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

                # Extrato detalhado
                elements.append(Paragraph("Extrato Detalhado", styles['Heading2']))
                cols_pdf = ['Data', 'Fazenda', 'Categoria', 'Evento', 'Entrada (+)', 'Saída (-)']
                data_ext = [cols_pdf]
                for _, row in df_extrato.iterrows():
                    data_ext.append([
                        row['Data'], row['Fazenda'], row['Categoria'],
                        row['Evento'][:30], str(int(row['Entrada (+)'])), str(int(row['Saída (-)']))
                    ])
                t2 = Table(data_ext, hAlign='LEFT',
                           colWidths=[50, 70, 90, 130, 50, 50])
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
    # NOVA TELA v3.2: KPIs ZOOTÉCNICOS (SEMÁFORO)
    # =========================================================
    # MAPEAMENTO DE CATEGORIAS → GRUPOS DE KPI:
    #
    #   Bezerros (0-60 dias) → "Mamando - Machos" + "Mamando - Fêmeas"
    #   Recria (2-12 meses)  → "Novilhas até 1 ano"
    #   Novilhas (>12 meses) → "Novilhas de 1 a 2 anos" + "Novilhas Prenhas"
    #   Vacas Adultas        → "Vacas Lactantes" + "Vacas Secas" +
    #                          "Vacas a refugar" + "Vacas refugadas"
    #
    # DENOMINADOR: Total do período = Saldo Inicial + Entradas do período
    # ÓBITOS:      evento = 'Saída/Mortes' dentro do período filtrado
    #
    # SEMÁFORO (conforme especificação do cliente):
    #   Verde    = dentro da meta ideal
    #   Amarelo  = aceitável (atenção)
    #   Vermelho = crítico (alerta imediato)
    # =========================================================
    elif menu == "📊 KPIs Zootécnicos":
        st.header("📊 KPIs Zootécnicos — Painel de Indicadores")

        conn = get_connection()
        fazendas = pd.read_sql(text("SELECT id_fazenda, nome_fazenda FROM fazendas"), conn)

        st.sidebar.divider()
        st.sidebar.subheader("Filtros dos KPIs")
        hoje = date.today()
        d_ini = st.sidebar.date_input("Data Inicial", date(hoje.year, hoje.month, 1), key="kpi_ini")
        d_fim = st.sidebar.date_input("Data Final", hoje, key="kpi_fim")
        faz_opcoes = ["Todas as Fazendas"] + fazendas['nome_fazenda'].tolist()
        faz_sel = st.sidebar.selectbox("Fazenda", faz_opcoes, key="kpi_faz")

        # Monta filtro de fazenda
        filtro_faz = ""
        params = {"d_ini": d_ini, "d_fim": d_fim}
        if faz_sel != "Todas as Fazendas":
            id_faz = int(fazendas[fazendas['nome_fazenda'] == faz_sel]['id_fazenda'].values[0])
            filtro_faz = "AND id_fazenda = :id_faz"
            params["id_faz"] = id_faz

        # ----------------------------------------------------------
        # FUNÇÃO AUXILIAR: calcula óbitos e população de um grupo
        # ----------------------------------------------------------
        def calc_kpi_grupo(conn, categorias: list, params: dict, filtro_faz: str):
            cats_sql = ", ".join([f"'{c}'" for c in categorias])

            # Óbitos no período
            q_obitos = text(f"""
                SELECT COALESCE(SUM(quantidade), 0)
                FROM lanc_estoque
                WHERE evento = 'Saída/Mortes'
                  AND categoria IN ({cats_sql})
                  AND data_movimento BETWEEN :d_ini AND :d_fim
                  {filtro_faz}
            """)
            obitos = conn.execute(q_obitos, params).scalar() or 0

            # Saldo inicial (acumulado antes de d_ini)
            q_saldo_ini = text(f"""
                SELECT COALESCE(SUM(
                    CASE WHEN evento LIKE 'Entrada%%' OR evento LIKE 'Transferências/De%%'
                         THEN quantidade ELSE -quantidade END
                ), 0)
                FROM lanc_estoque
                WHERE categoria IN ({cats_sql})
                  AND data_movimento < :d_ini
                  {filtro_faz}
            """)
            saldo_ini = conn.execute(q_saldo_ini, params).scalar() or 0

            # Entradas no período
            q_entradas = text(f"""
                SELECT COALESCE(SUM(quantidade), 0)
                FROM lanc_estoque
                WHERE (evento LIKE 'Entrada%%' OR evento LIKE 'Transferências/De%%')
                  AND categoria IN ({cats_sql})
                  AND data_movimento BETWEEN :d_ini AND :d_fim
                  {filtro_faz}
            """)
            entradas = conn.execute(q_entradas, params).scalar() or 0

            populacao = saldo_ini + entradas
            taxa = (obitos / populacao * 100) if populacao > 0 else 0.0
            return int(obitos), int(populacao), round(taxa, 2)

        # ----------------------------------------------------------
        # FUNÇÃO DO SEMÁFORO
        # ----------------------------------------------------------
        def semaforo(taxa, verde_max, amarelo_max):
            """Retorna (emoji, cor_hex, label) conforme os limites."""
            if taxa < verde_max:
                return "🟢", "#27ae60", "IDEAL"
            elif taxa <= amarelo_max:
                return "🟡", "#f39c12", "ATENÇÃO"
            else:
                return "🔴", "#e74c3c", "CRÍTICO"

        # ----------------------------------------------------------
        # CÁLCULO DOS 4 KPIs DE MORTALIDADE
        # ----------------------------------------------------------
        grupos_mortalidade = {
            "Bezerros (0–60 dias)": {
                "categorias": ["Mamando - Machos", "Mamando - Fêmeas"],
                "verde_max": 3.0, "amarelo_max": 5.0,
                "descricao": "Sobrevivência na fase mais crítica da vida."
            },
            "Recria (2–12 meses)": {
                "categorias": ["Novilhas até 1 ano"],
                "verde_max": 3.0, "amarelo_max": 5.0,
                "descricao": "Saúde dos animais em desenvolvimento."
            },
            "Novilhas (>12 meses)": {
                "categorias": ["Novilhas de 1 a 2 anos", "Novilhas Prenhas"],
                "verde_max": 1.0, "amarelo_max": 2.0,
                "descricao": "Fêmeas jovens aptas à reprodução."
            },
            "Vacas Adultas": {
                "categorias": ["Vacas Lactantes", "Vacas Secas",
                               "Vacas a refugar", "Vacas refugadas"],
                "verde_max": 3.0, "amarelo_max": 5.0,
                "descricao": "Perda do ativo principal gerador de receita."
            },
        }

        st.subheader(f"🚦 Taxas de Mortalidade — {d_ini.strftime('%d/%m/%Y')} a {d_fim.strftime('%d/%m/%Y')}")
        st.caption(f"Fazenda: **{faz_sel}** | Denominador: Saldo Inicial + Entradas do período")

        # Grade 2x2 para os cards de mortalidade
        col_pares = [st.columns(2), st.columns(2)]
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
                    <div style="font-size:2.4rem; font-weight:900; color:{cor};">{taxa:.2f}%</div>
                    <div style="font-size:0.85rem; color:#555;">
                        {obitos} óbitos / {pop} animais no período
                    </div>
                    <div style="margin-top:6px;">
                        <span style="background:{cor}; color:white; padding:2px 10px;
                                     border-radius:20px; font-size:0.8rem; font-weight:700;">
                            {label}
                        </span>
                    </div>
                    <div style="font-size:0.78rem; color:#777; margin-top:8px;">{cfg['descricao']}</div>
                    <div style="font-size:0.75rem; color:#aaa; margin-top:4px;">
                        Meta: &lt;{cfg['verde_max']}% ✅ | Atenção: até {cfg['amarelo_max']}% ⚠️
                    </div>
                </div>
                """, unsafe_allow_html=True)

        st.divider()

        # ----------------------------------------------------------
        # KPIs DE COMPOSIÇÃO DO REBANHO
        # ----------------------------------------------------------
        st.subheader("🐄 Composição e Eficiência do Rebanho")

        # Busca saldo acumulado até d_fim por categoria
        q_saldo = text(f"""
            SELECT categoria,
                   SUM(CASE WHEN evento LIKE 'Entrada%%' OR evento LIKE 'Transferências/De%%'
                            THEN quantidade ELSE -quantidade END) as saldo
            FROM lanc_estoque
            WHERE data_movimento <= :d_fim
            {filtro_faz}
            GROUP BY categoria
        """)
        df_saldo = pd.read_sql(q_saldo, conn, params={"d_fim": d_fim, **({
            "id_faz": params["id_faz"]} if "id_faz" in params else {})})
        saldo_dict = dict(zip(df_saldo['categoria'], df_saldo['saldo'].clip(lower=0)))

        lactantes  = saldo_dict.get("Vacas Lactantes", 0)
        secas      = saldo_dict.get("Vacas Secas", 0)
        total_rebanho = sum(v for v in saldo_dict.values() if v > 0)
        total_vacas = lactantes + secas

        pct_lact_vacas    = (lactantes / total_vacas * 100)    if total_vacas > 0    else 0.0
        pct_lact_rebanho  = (lactantes / total_rebanho * 100)  if total_rebanho > 0  else 0.0

        # Semáforos de composição (metas invertidas: abaixo é pior)
        def semaforo_comp(valor, verde_min, amarelo_min):
            if valor >= verde_min:
                return "🟢", "#27ae60", "IDEAL"
            elif valor >= amarelo_min:
                return "🟡", "#f39c12", "ATENÇÃO"
            else:
                return "🔴", "#e74c3c", "CRÍTICO"

        em1, cor1, lb1 = semaforo_comp(pct_lact_vacas,   83.0, 75.0)
        em2, cor2, lb2 = semaforo_comp(pct_lact_rebanho, 55.0, 50.0)

        c1, c2 = st.columns(2)
        for col, emoji, cor, label, titulo, valor, meta_v, meta_a, descricao in [
            (c1, em1, cor1, lb1,
             "% Lactantes / Total de Vacas", pct_lact_vacas,
             "≥ 83%", "75% a 82%",
             f"Lactantes: {int(lactantes)} | Secas: {int(secas)} | Total Vacas: {int(total_vacas)}"),
            (c2, em2, cor2, lb2,
             "% Lactantes / Rebanho Total", pct_lact_rebanho,
             "≥ 55%", "50% a 54%",
             f"Lactantes: {int(lactantes)} | Rebanho Total: {int(total_rebanho)} animais"),
        ]:
            with col:
                st.markdown(f"""
                <div style="border:2px solid {cor}; border-radius:12px; padding:16px;
                            background:{'#f9fbe7' if cor=='#f39c12' else ('#fdecea' if cor=='#e74c3c' else '#f0faf4')};">
                    <div style="font-size:1.1rem; font-weight:700; color:#2c3e50;">{emoji} {titulo}</div>
                    <div style="font-size:2.4rem; font-weight:900; color:{cor};">{valor:.1f}%</div>
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

        # ----------------------------------------------------------
        # TABELA RESUMO DOS KPIs
        # ----------------------------------------------------------
        st.subheader("📋 Tabela Resumo — Todos os Indicadores")
        rows = []
        for r in kpi_results:
            rows.append({
                "Indicador":   r["nome"],
                "Óbitos":      r["obitos"],
                "População":   r["pop"],
                "Taxa (%)":    r["taxa"],
                "Status":      f"{r['emoji']} {r['label']}",
                "Meta Verde":  f"< {r['verde_max']}%",
                "Limite Crítico": f"> {r['amarelo_max']}%",
            })
        rows.append({
            "Indicador":   "% Lactantes / Total Vacas",
            "Óbitos":      "-",
            "População":   int(total_vacas),
            "Taxa (%)":    round(pct_lact_vacas, 2),
            "Status":      f"{em1} {lb1}",
            "Meta Verde":  "≥ 83%",
            "Limite Crítico": "< 75%",
        })
        rows.append({
            "Indicador":   "% Lactantes / Rebanho Total",
            "Óbitos":      "-",
            "População":   int(total_rebanho),
            "Taxa (%)":    round(pct_lact_rebanho, 2),
            "Status":      f"{em2} {lb2}",
            "Meta Verde":  "≥ 55%",
            "Limite Crítico": "< 50%",
        })
        df_resumo = pd.DataFrame(rows)
        st.dataframe(df_resumo, use_container_width=True, hide_index=True)

        # Exportação Excel
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
            conn.execute(text("""
                INSERT INTO fechamentos_mensais (ano_mes, status) VALUES (:m, 'Fechado')
                ON CONFLICT (ano_mes) DO UPDATE SET status = 'Fechado'
            """), {"m": mes})
            conn.commit()
            conn.close()
            st.success(f"Mês {mes.strftime('%m/%Y')} fechado!")

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
