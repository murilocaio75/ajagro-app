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
        "📋 Módulo de Auditoria",       # NOVO em v3.2
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
                .applymap(highlight_saldo, subset=['Saldo Final'])
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
                        row['Evento'][:30], str(int(row['Entrada (-)'])), str(int(row['Saída (-)']))
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
    # TELA: FECHAMENTO MENSAL
    # =========================================================
    elif menu == "Fechamento Mensal":
    st.header("🔒 Fechamento Contábil")
    mes = st.date_input("Mês para Fechar", date.today().replace(day=1))
    
    # Sempre normaliza para o primeiro dia, independente do que foi selecionado
    mes_normalizado = mes.replace(day=1)
    
    if mes != mes_normalizado:
        st.warning(f"A data será ajustada para o primeiro dia do mês: {mes_normalizado.strftime('%d/%m/%Y')}")
    
    st.info(f"Mês a ser fechado: **{mes_normalizado.strftime('%m/%Y')}**")
    
    if st.button("Executar Fechamento"):
        conn = get_connection()
        conn.execute(text("""
            INSERT INTO fechamentos_mensais (ano_mes, status) VALUES (:m, 'Fechado')
            ON CONFLICT (ano_mes) DO UPDATE SET status = 'Fechado'
        """), {"m": mes_normalizado})  # ← sempre o primeiro dia
        conn.commit()
        conn.close()
        st.success(f"Mês {mes_normalizado.strftime('%m/%Y')} fechado!")

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