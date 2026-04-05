import streamlit as st
import pandas as pd
from sqlalchemy import create_engine, text
from datetime import date
import io
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

# --- CONFIGURAÇÃO DA PÁGINA ---
st.set_page_config(page_title="AJAGRO - Gestão Pecuária 3.0", layout="wide")

# --- CONEXÃO ---
def get_connection():
    db_url = st.secrets["DB_CONN_STRING"].replace("postgres://", "postgresql+psycopg2://", 1)
    return create_engine(db_url).connect()

# --- CATEGORIAS OFICIAIS (ITEM 6) ---
CATEGORIAS_LISTA = [
    "Vacas Lactantes", "Vacas Secas", "Vacas a refugar", "Vacas refugadas",
    "Mamando - Machos", "Mamando - Femeas", "Novilhas até 1 ano",
    "Novilhas de 1 a 2 anos", "Novilhas Prenhas", "Machos"
]

EVENTOS_ENTRADA = ["Entrada/Nascimento", "Entrada/Compras", "Entrada/Outros", "Entrada/Outros/Parcerias", "Transferências/De Outras Categorias", "Transferências/De Outras Fazendas"]
EVENTOS_SAIDA = ["Saída/Vendas Comerciais", "Saída/Vendas Descartes", "Saída/Abates Comerciais", "Saída/Mortes", "Saída/Doações Extern", "Saída/Doações Intern", "Transferências/Para Outras Categorias", "Transferências/Para Outras Fazendas"]

# --- FUNÇÕES DE VALIDAÇÃO ---
def get_saldo_atual(conn, fazenda_id, categoria):
    query = text("""
        SELECT SUM(CASE WHEN evento LIKE 'Entrada%%' OR evento LIKE 'Transferências/De%%' THEN quantidade ELSE -quantidade END) 
        FROM lanc_estoque WHERE id_fazenda = :f AND categoria = :c
    """)
    res = conn.execute(query, {"f": fazenda_id, "c": categoria}).scalar()
    return res if res else 0

def is_mes_fechado(conn, data_mov):
    prim_dia = data_mov.replace(day=1)
    res = conn.execute(text("SELECT status FROM fechamentos_mensais WHERE ano_mes = :d"), {"d": prim_dia}).scalar()
    return res == 'Fechado'

# --- SISTEMA DE LOGIN ---
if "autenticado" not in st.session_state:
    st.session_state["autenticado"] = False

if not st.session_state["autenticado"]:
    st.markdown("<h1 style='text-align: center;'>🔐 AJAGRO 3.0 - Acesso</h1>", unsafe_allow_html=True)
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
    # --- MENU LATERAL ---
    st.sidebar.title("Módulo MEG - AJAGRO")
    menu = st.sidebar.selectbox("Ir para:", ["Dashboard & Balanço", "Lançamento de Eventos", "Cadastros Base", "Fechamento Mensal"])
    
    if st.sidebar.button("🚪 Sair do Sistema"):
        st.session_state["autenticado"] = False
        st.rerun()

    # --- TELA: LANÇAMENTO DE EVENTOS ---
    if menu == "Lançamento de Eventos":
        st.header("📝 Registro de Eventos")
        conn = get_connection()
        fazendas = pd.read_sql(text("SELECT id_fazenda, nome_fazenda FROM fazendas"), conn)
        
        if fazendas.empty:
            st.warning("Cadastre uma fazenda primeiro em 'Cadastros Base'.")
        else:
            with st.form("form_evento", clear_on_submit=True):
                col1, col2 = st.columns(2)
                with col1:
                    data_mov = st.date_input("Data do Evento", date.today())
                    faz_sel = st.selectbox("Fazenda", fazendas['nome_fazenda'])
                    evento_sel = st.selectbox("Tipo de Evento", EVENTOS_ENTRADA + EVENTOS_SAIDA)
                
                with col2:
                    # Regra 3.1: Filtro de categoria para Nascimentos
                    if evento_sel == "Entrada/Nascimento":
                        cat_opcoes = ["Mamando - Machos", "Mamando - Femeas"]
                    else:
                        cat_opcoes = CATEGORIAS_LISTA
                    
                    cat_sel = st.selectbox("Categoria", cat_opcoes)
                    qtd = st.number_input("Quantidade de Cabeças", min_value=1, step=1)
                
                obs = st.text_area("Observações (Obrigatório para Nascimentos e Saídas)")
                
                if st.form_submit_button("Confirmar Lançamento"):
                    id_f = int(fazendas[fazendas['nome_fazenda'] == faz_sel]['id_fazenda'].values[0])
                    
                    if is_mes_fechado(conn, data_mov):
                        st.error("Este mês já foi FECHADO e não permite novos lançamentos.")
                    elif evento_sel == "Entrada/Nascimento" and not obs:
                        st.error("Para nascimentos, descreva se o parto foi Multípara ou Primípara e o ID.")
                    else:
                        # Regra 8.1: Checagem de Saldo para Saídas
                        pode_gravar = True
                        if evento_sel in EVENTOS_SAIDA:
                            saldo = get_saldo_atual(conn, id_f, cat_sel)
                            if saldo < qtd:
                                st.error(f"Saldo insuficiente! Saldo atual de {cat_sel}: {saldo} cab.")
                                pode_gravar = False
                        
                        if pode_gravar:
                            sql = text("INSERT INTO lanc_estoque (data_movimento, id_fazenda, quantidade, evento, categoria, observacao) VALUES (:d, :f, :q, :e, :c, :o)")
                            conn.execute(sql, {"d": data_mov, "f": id_f, "q": qtd, "e": evento_sel, "c": cat_sel, "o": obs})
                            conn.commit()
                            st.success("Evento registrado com sucesso!")
        conn.close()

    # --- TELA: DASHBOARD & BALANÇO ---
    elif menu == "Dashboard & Balanço":
        st.header("📊 Balanço e Evolução")
        conn = get_connection()
        
        # Balanço Acumulado (Item 10)
        st.sidebar.subheader("Período")
        d_fim = st.sidebar.date_input("Data do Saldo", date.today())
        
        q_balanco = text("""
            SELECT categoria as "Categoria", 
            SUM(CASE WHEN evento LIKE 'Entrada%%' OR evento LIKE 'Transferências/De%%' THEN quantidade ELSE -quantidade END) as "Estoque"
            FROM lanc_estoque WHERE data_movimento <= :f
            GROUP BY categoria
        """)
        df_bal = pd.read_sql(q_balanco, conn, params={"f": d_fim})
        
        c1, c2 = st.columns([2, 1])
        with c1:
            st.subheader(f"Estoque em {d_fim.strftime('%d/%m/%Y')}")
            st.dataframe(df_bal, use_container_width=True, hide_index=True)
        
        with c2:
            st.subheader("Exportar Dados")
            # Excel
            buf_xlsx = io.BytesIO()
            df_bal.to_excel(buf_xlsx, index=False)
            st.download_button("📥 Baixar Excel", buf_xlsx.getvalue(), "balanco_ajagro.xlsx")
            
            # PDF Simples (Item 11)
            pdf_buf = io.BytesIO()
            canv = canvas.Canvas(pdf_buf, pagesize=letter)
            canv.drawString(50, 750, f"AJAGRO - BALANÇO PATRIMONIAL EM {d_fim}")
            y_pos = 720
            for i, row in df_bal.iterrows():
                canv.drawString(50, y_pos, f"{row['Categoria']}: {row['Estoque']} cab.")
                y_pos -= 20
            canv.save()
            st.download_button("📥 Baixar PDF", pdf_buf.getvalue(), "balanco_ajagro.pdf")

        # Histórico de Lançamentos (Item 2.1)
        st.divider()
        st.subheader("📑 Últimos Lançamentos (Histórico)")
        df_hist = pd.read_sql(text("SELECT TO_CHAR(data_movimento, 'DD/MM/YYYY') as Data, evento as Evento, categoria as Categoria, quantidade as Quantidade FROM lanc_estoque ORDER BY id_lancamento DESC LIMIT 20"), conn)
        st.table(df_hist)
        conn.close()

    # --- TELA: CADASTROS BASE ---
    elif menu == "Cadastros Base":
        st.header("🏢 Cadastro de Fazendas")
        with st.form("add_fazenda"):
            n = st.text_input("Nome da Fazenda")
            c = st.text_input("CNPJ/CPF")
            if st.form_submit_button("Salvar Fazenda"):
                conn = get_connection()
                conn.execute(text("INSERT INTO fazendas (nome_fazenda, cnpj_cpf) VALUES (:n, :c)"), {"n": n, "c": c})
                conn.commit()
                conn.close()
                st.success("Fazenda cadastrada!")

    # --- TELA: FECHAMENTO MENSAL (ITEM 9) ---
    elif menu == "Fechamento Mensal":
        st.header("🔒 Fechamento Contábil")
        st.info("O fechamento impede edições retroativas para garantir a auditoria.")
        mes = st.date_input("Mês para Fechar", date.today().replace(day=1))
        if st.button("Executar Fechamento"):
            conn = get_connection()
            conn.execute(text("INSERT INTO fechamentos_mensais (ano_mes, status) VALUES (:m, 'Fechado') ON CONFLICT (ano_mes) DO UPDATE SET status = 'Fechado'"), {"m": mes})
            conn.commit()
            conn.close()
            st.success(f"Mês {mes.strftime('%m/%Y')} fechado com sucesso!")
            