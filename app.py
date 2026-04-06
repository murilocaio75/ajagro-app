import streamlit as st
import pandas as pd
from sqlalchemy import create_engine, text
from datetime import date
import io
import plotly.express as px
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

# --- CONFIGURAÇÃO DA PÁGINA ---
st.set_page_config(page_title="AJAGRO - Gestão Pecuária 3.1", layout="wide")

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

EVENTOS_ENTRADA = ["Entrada/Nascimento", "Entrada/Compras", "Entrada/Outros", "Entrada/Outros/Parcerias", "Transferências/De Outras Categorias", "Transferências/De Outras Fazendas"]
EVENTOS_SAIDA = ["Saída/Vendas Comerciais", "Saída/Vendas Descartes", "Saída/Abates Comerciais", "Saída/Mortes", "Saída/Doações Extern", "Saída/Doações Intern", "Transferências/Para Outras Categorias", "Transferências/Para Outras Fazendas"]

# --- FUNÇÕES DE APOIO ---
def get_saldo_atual(conn, fazenda_id, categoria):
    query = text("""
        SELECT SUM(CASE WHEN evento LIKE 'Entrada%%' OR evento LIKE 'Transferências/De%%' THEN quantidade ELSE -quantidade END) 
        FROM lanc_estoque WHERE id_fazenda = :f AND categoria = :c
    """)
    res = conn.execute(query, {"f": fazenda_id, "c": categoria}).scalar()
    return res if res else 0

def is_mes_fechado(conn, data_mov):
    # Forçamos a data para o primeiro dia do mês e transformamos em string ISO
    # Exemplo: 25/03/2026 vira '2026-03-01'
    primeiro_dia_iso = data_mov.replace(day=1).strftime('%Y-%m-%d')
    
    # Comparamos como string no banco de dados para evitar erros de fuso horário
    query = text("""
        SELECT status FROM fechamentos_mensais 
        WHERE TO_CHAR(ano_mes, 'YYYY-MM-DD') = :d AND status = 'Fechado'
    """)
    
    try:
        res = conn.execute(query, {"d": primeiro_dia_iso}).fetchone()
        return res is not None
    except:
        return False

# --- LOGIN ---
if "autenticado" not in st.session_state:
    st.session_state["autenticado"] = False

if not st.session_state["autenticado"]:
    st.markdown("<h1 style='text-align: center;'>🔐 AJAGRO 3.1 - Acesso</h1>", unsafe_allow_html=True)
    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        with st.form("login"):
            u = st.text_input("Usuário")
            p = st.text_input("Senha", type="password")
            if st.form_submit_button("Entrar"):
                if u == st.secrets["USER_ADMIN"] and p == st.secrets["PASS_ADMIN"]:
                    st.session_state["autenticado"] = True
                    st.rerun()
                else: st.error("Credenciais inválidas")
else:
    st.sidebar.title("Módulo MEG - AJAGRO")
    menu = st.sidebar.selectbox("Ir para:", ["Dashboard & Balanço", "Lançamento de Eventos", "Cadastros Base", "Fechamento Mensal", "⚙️ Ajuste de Preços"])
    
    if st.sidebar.button("🚪 Sair do Sistema"):
        st.session_state["autenticado"] = False
        st.rerun()

# --- TELA: LANÇAMENTO DE EVENTOS (VERSÃO CORRIGIDA) ---
    elif menu == "Lançamento de Eventos":
        st.header("📝 Registro de Eventos")
        conn = get_connection()
        fazendas = pd.read_sql(text("SELECT id_fazenda, nome_fazenda FROM fazendas"), conn)
        
        if fazendas.empty:
            st.warning("Cadastre uma fazenda primeiro em 'Cadastros Base'.")
        else:
            # Note que removemos o 'with st.form' para permitir a atualização em tempo real
            col1, col2 = st.columns(2)
            
            with col1:
                data_mov = st.date_input("Data do Evento", date.today())
                faz_sel = st.selectbox("Fazenda", fazendas['nome_fazenda'])
                evento_sel = st.selectbox("Tipo de Evento", EVENTOS_ENTRADA + EVENTOS_SAIDA)
            
            with col2:
                # Agora a lógica condicional reage na hora à mudança do 'evento_sel'
                if evento_sel == "Entrada/Nascimento":
                    cat_opcoes = ["Mamando - Machos", "Mamando - Femeas"]
                else:
                    cat_opcoes = CATEGORIAS_LISTA
                
                cat_sel = st.selectbox("Categoria", cat_opcoes)
                qtd = st.number_input("Quantidade de Cabeças", min_value=1, step=1)

            obs = st.text_area("Observações (Obrigatório para Nascimentos e Saídas)")

# --- NOVO BLOCO DO BOTÃO: TRANSFERÊNCIA ATÔMICA E FLUXO ---
            if st.button("Confirmar Lançamento"):
                id_f = int(fazendas[fazendas['nome_fazenda'] == faz_sel]['id_fazenda'].values[0])
                
                # Definição do Fluxo Obrigatório de Transferência (Regra 8.4)
                fluxo_transferencia = {
                    "Mamando - Machos": "Machos",
                    "Mamando - Fêmeas": "Novilhas até 1 ano",
                    "Novilhas até 1 ano": "Novilhas de 1 a 2 anos",
                    "Novilhas de 1 a 2 anos": "Novilhas Prenhas",
                    "Novilhas Prenhas": "Vacas Lactantes",
                    "Vacas Lactantes": "Vacas Secas"
                }

                if is_mes_fechado(conn, data_mov):
                    st.error("Este mês já foi FECHADO e não permite novos lançamentos.")
                elif (evento_sel == "Entrada/Nascimento" or evento_sel in EVENTOS_SAIDA) and not obs:
                    st.error("Para este evento, o campo observação é obrigatório.")
                else:
                    pode_gravar = True
                    
                    # Validação de Saldo para Saídas e Transferências (Regra 8.1)
                    if evento_sel in EVENTOS_SAIDA or evento_sel == "Transferências/Para Outras Categorias":
                        saldo_atual = get_saldo_atual(conn, id_f, cat_sel)
                        if saldo_atual < qtd:
                            st.error(f"Saldo insuficiente! Estoque de {cat_sel}: {saldo_atual} cab.")
                            pode_gravar = False
                    
                    if pode_gravar:
                        try:
                            # Operação para Transferências (Duplo Lançamento)
                            if evento_sel == "Transferências/Para Outras Categorias":
                                cat_destino = fluxo_transferencia.get(cat_sel)
                                
                                if not cat_destino:
                                    st.error(f"A categoria '{cat_sel}' não possui um destino definido no fluxo.")
                                else:
                                    # 1. Saída da Categoria Atual
                                    sql_sai = text("INSERT INTO lanc_estoque (data_movimento, id_fazenda, quantidade, evento, categoria, observacao) VALUES (:d, :f, :q, :e, :c, :o)")
                                    conn.execute(sql_sai, {"d": data_mov, "f": id_f, "q": qtd, "e": "Transferências/Para Outras Categorias", "c": cat_sel, "o": f"Saída p/ {cat_destino}. Obs: {obs}"})
                                    
                                    # 2. Entrada na Categoria de Destino (Automática)
                                    sql_ent = text("INSERT INTO lanc_estoque (data_movimento, id_fazenda, quantidade, evento, categoria, observacao) VALUES (:d, :f, :q, :e, :c, :o)")
                                    conn.execute(sql_ent, {"d": data_mov, "f": id_f, "q": qtd, "e": "Transferências/De Outras Categorias", "c": cat_destino, "o": f"Entrada vinda de {cat_sel}. Obs: {obs}"})
                                    
                                    conn.commit()
                                    st.success(f"Transferência concluída! {qtd} cab. saíram de {cat_sel} e entraram em {cat_destino}.")
                                    st.balloons()
                            
                            else:
                                # Lançamentos normais (Entradas, Vendas, Mortes)
                                sql = text("INSERT INTO lanc_estoque (data_movimento, id_fazenda, quantidade, evento, categoria, observacao) VALUES (:d, :f, :q, :e, :c, :o)")
                                conn.execute(sql, {"d": data_mov, "f": id_f, "q": qtd, "e": evento_sel, "c": cat_sel, "o": obs})
                                conn.commit()
                                st.success(f"Sucesso! {qtd} '{cat_sel}' registrado como '{evento_sel}'.")
                                st.balloons()
                        
                        except Exception as e:
                            st.error(f"Erro no banco: {e}")

        # --- IMPORTANTE: Manter o fechamento da conexão aqui ---
        conn.close()

    # --- TELA: DASHBOARD & BALANÇO ---
    elif menu == "Dashboard & Balanço":
        st.header("📊 Balanço e Evolução Patrimonial")
        conn = get_connection()
        
        # CORREÇÃO 2: Filtros de Data Inicial e Final
        st.sidebar.divider()
        st.sidebar.subheader("Período do Dashboard")
        hoje = date.today()
        d_ini = st.sidebar.date_input("Data Inicial", date(hoje.year, hoje.month, 1))
        d_fim = st.sidebar.date_input("Data Final", hoje)

        # Cálculo do Estoque (Saldo acumulado até Data Final)
        q_balanco = text("""
            SELECT categoria as "Categoria", 
            SUM(CASE WHEN evento LIKE 'Entrada%%' OR evento LIKE 'Transferências/De%%' THEN quantidade ELSE -quantidade END) as "Estoque"
            FROM lanc_estoque WHERE data_movimento <= :f
            GROUP BY categoria
        """)
        df_bal = pd.read_sql(q_balanco, conn, params={"f": d_fim})
        
        if not df_bal.empty:
            # Integração com Preços para Valorização
            query_p = text("SELECT * FROM precos_gestao")
            df_p = pd.read_sql(query_p, conn)
            dict_p = dict(zip(df_p['categoria'], df_p['valor']))
            
            df_bal['Preço Unit.'] = df_bal['Categoria'].map(dict_p).fillna(0)
            df_bal['Total R$'] = df_bal['Estoque'] * df_bal['Preço Unit.']
            
            # Métricas
            total_r = df_bal['Total R$'].sum()
            total_c = df_bal['Estoque'].sum()
            m1, m2, m3 = st.columns(3)
            m1.metric("Estoque Total", f"{int(total_c)} cab.")
            m2.metric("Valorização Total", f"R$ {total_r:,.2f}")
            m3.metric("Média por Animal", f"R$ {(total_r/total_c if total_c > 0 else 0):,.2f}")

            st.divider()
            col_g, col_t = st.columns([1, 1.2])
            
            with col_g:
                # CORREÇÃO: Retorno do Gráfico de Pizza
                st.subheader("Distribuição do Patrimônio")
                fig = px.pie(df_bal[df_bal['Total R$'] > 0], values='Total R$', names='Categoria', hole=0.4)
                st.plotly_chart(fig, use_container_width=True)
            
            with col_t:
                st.subheader("Tabela de Participação")
                st.dataframe(df_bal.style.format({'Preço Unit.': 'R$ {:.2f}', 'Total R$': 'R$ {:.2f}'}), use_container_width=True, hide_index=True)
                
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

        # Histórico de Lançamentos (CORREÇÃO 3: Data em padrão BR)
        st.divider()
        st.subheader("📑 Últimos Lançamentos")
        df_hist = pd.read_sql(text("SELECT TO_CHAR(data_movimento, 'DD/MM/YYYY') as Data, evento as Evento, categoria as Categoria, quantidade as Quantidade FROM lanc_estoque ORDER BY id_lancamento DESC LIMIT 20"), conn)
        st.table(df_hist)
        conn.close()

    # --- OUTRAS TELAS MANTIDAS ---
    elif menu == "Cadastros Base":
        st.header("🏢 Cadastro de Fazendas")
        with st.form("add_f"):
            n = st.text_input("Nome da Fazenda")
            c = st.text_input("CNPJ/CPF")
            if st.form_submit_button("Salvar"):
                conn = get_connection()
                conn.execute(text("INSERT INTO fazendas (nome_fazenda, cnpj_cpf) VALUES (:n, :c)"), {"n": n, "c": c})
                conn.commit()
                conn.close()
                st.success("Fazenda cadastrada!")

    elif menu == "Fechamento Mensal":
        st.header("🔒 Fechamento Contábil")
        mes = st.date_input("Mês para Fechar", date.today().replace(day=1))
        if st.button("Executar Fechamento"):
            conn = get_connection()
            conn.execute(text("INSERT INTO fechamentos_mensais (ano_mes, status) VALUES (:m, 'Fechado') ON CONFLICT (ano_mes) DO UPDATE SET status = 'Fechado'"), {"m": mes})
            conn.commit()
            conn.close()
            st.success(f"Mês {mes.strftime('%m/%Y')} fechado!")

    elif menu == "⚙️ Ajuste de Preços":
        st.header("⚙️ Ajuste de Preços")
        conn = get_connection()
        p_df = pd.read_sql(text("SELECT * FROM precos_gestao"), conn)
        with st.form("p"):
            dict_n = {}
            for cat in CATEGORIAS_LISTA:
                val_atual = p_df[p_df['categoria'] == cat]['valor'].values[0] if cat in p_df['categoria'].values else 0.0
                dict_n[cat] = st.number_input(f"{cat} (R$)", value=float(val_atual))
            if st.form_submit_button("Salvar Preços"):
                for c, v in dict_n.items():
                    conn.execute(text("INSERT INTO precos_gestao (categoria, valor) VALUES (:c, :v) ON CONFLICT (categoria) DO UPDATE SET valor = EXCLUDED.valor"), {"c": c, "v": v})
                conn.commit()
                st.success("Preços atualizados!")
        conn.close()

        