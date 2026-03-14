import streamlit as st
import pandas as pd
import sqlite3
import plotly.express as px
from datetime import date


# Configuração da Página
st.set_page_config(page_title="Sistema de Gestão de Pecuária - AJAGRO", layout="wide")

def get_connection():
    return sqlite3.connect('ajagro.db')

# --- NOVO: LÓGICA DE PREÇOS DINÂMICOS ---
def carregar_precos():
    conn = get_connection()
    # Tenta ler do banco, se não existir ou estiver vazio, usa os padrões
    try:
        df = pd.read_sql("SELECT * FROM precos_gestao", conn)
        if df.empty:
            raise Exception("Tabela vazia")
        precos = dict(zip(df['categoria'], df['valor']))
    except:
        # Padrões iniciais (os mesmos que você já usava)
        precos = {
            "Vacas Lactantes": 5000.00, "Vacas Secas": 5000.00,
            "Vacas a refugar": 1000.00, "Vacas refugadas": 1500.00,
            "Mamando - Machos": 1000.00, "Mamando - Fêmeas": 1000.00,
            "Novilhas até 1 ano": 2000.00, "Novilhas de 1 a 2 anos": 3000.00,
            "Novilhas Prenhas": 5000.00, "Machos": 4000.00
        }
    conn.close()
    return precos

# Inicializa a variável com os preços atuais
CATEGORIAS_PRECOS = carregar_precos()

# --- NAVEGAÇÃO LATERAL ---
st.sidebar.title("Módulo MEG - AJAGRO")
menu = st.sidebar.selectbox("Ir para:", ["Dashboard & Valorização", "Cadastros Base", "Lançamento de Estoque", "⚙️ Ajuste de Preços"])

# --- PÁGINA: CADASTROS BASE ---
if menu == "Cadastros Base":
    st.header("🏢 Cadastros de Base")
    aba1, aba2 = st.tabs(["Fazendas", "Lotes"])
    
    with aba1:
        with st.form("form_fazenda"):
            nome = st.text_input("Nome da Fazenda")
            doc = st.text_input("CNPJ/CPF")
            if st.form_submit_button("Cadastrar Fazenda"):
                conn = get_connection()
                conn.execute("INSERT INTO fazendas (nome_fazenda, cnpj_cpf) VALUES (?,?)", (nome, doc))
                conn.commit()
                st.success(f"Fazenda {nome} cadastrada!")

    with aba2:
        conn = get_connection()
        fazendas_df = pd.read_sql("SELECT * FROM fazendas", conn)
        if not fazendas_df.empty:
            with st.form("form_lote"):
                fazenda_sel = st.selectbox("Selecione a Fazenda", fazendas_df['nome_fazenda'])
                id_fazenda = int(fazendas_df[fazendas_df['nome_fazenda'] == fazenda_sel]['id_fazenda'].values[0])
                desc_lote = st.text_input("Descrição do Lote (Ex: Piquete 01)")
                if st.form_submit_button("Cadastrar Lote"):
                    conn.execute("INSERT INTO lotes (id_fazenda, descricao) VALUES (?,?)", (id_fazenda, desc_lote))
                    conn.commit()
                    st.success("Lote cadastrado!")
        else:
            st.warning("Cadastre uma fazenda primeiro.")

# --- PÁGINA: LANÇAMENTO (Com Categoria para Valorização) ---
elif menu == "Lançamento de Estoque":
    st.header("📝 Movimentação de Gado")
    conn = get_connection()
    lotes_df = pd.read_sql("SELECT l.id_lote, l.descricao, f.nome_fazenda FROM lotes l JOIN fazendas f ON l.id_fazenda = f.id_fazenda", conn)

    if not lotes_df.empty:
        with st.form("form_movimentacao"):
            col1, col2 = st.columns(2)
            with col1:
                dt = st.date_input("Data do Movimento", date.today())
                lote_sel = st.selectbox("Lote/Fazenda", lotes_df['descricao'] + " - " + lotes_df['nome_fazenda'])
                cat = st.selectbox("Classe/Categoria (Valorização)", list(CATEGORIAS_PRECOS.keys()))
            with col2:
                tipo = st.selectbox("Operação", ["Entrada (Compra)", "Nascimento", "Morte", "Venda", "Transferência"])
                qtd = st.number_input("Quantidade de Cabeças", min_value=1)
            
            obs = st.text_area("Observações")
            if st.form_submit_button("Confirmar Lançamento"):
                id_lote = int(lotes_df[lotes_df['descricao'] + " - " + lotes_df['nome_fazenda'] == lote_sel]['id_lote'].values[0])
                conn.execute("""
                    INSERT INTO lanc_estoque (data_movimento, id_lote, quantidade, tipo_movimento, observacao, categoria) 
                    VALUES (?,?,?,?,?,?)""", (dt, id_lote, qtd, tipo, obs, cat))
                conn.commit()
                st.success("Movimentação registrada e valorizada no MEG!")
    else:
        st.error("Nenhum Lote ou Fazenda encontrado.")

# --- PÁGINA: DASHBOARD & VALORIZAÇÃO --- 
elif menu == "Dashboard & Valorização": # MUDAMOS DE 'else' PARA 'elif'
    st.header("📊 Painel Resumo & Valorização Patrimonial")
    conn = get_connection()

    # --- FILTROS NA BARRA LATERAL (ITEM 3) ---
    st.sidebar.divider()
    st.sidebar.subheader("Período de Análise")
    
    hoje = date.today()
    primeiro_dia = date(hoje.year, hoje.month, 1)
    
    data_inicio = st.sidebar.date_input("Data Inicial", primeiro_dia)
    data_fim = st.sidebar.date_input("Data Final", hoje)

    # 1. Cálculo de Saldos por Categoria
    query_filtrada = """
        SELECT 
            categoria,
            SUM(CASE WHEN tipo_movimento IN ('Entrada (Compra)', 'Nascimento', 'Transferência') THEN quantidade ELSE 0 END) -
            SUM(CASE WHEN tipo_movimento IN ('Morte', 'Venda') THEN quantidade ELSE 0 END) as saldo_qtd
        FROM lanc_estoque
        WHERE data_movimento BETWEEN ? AND ?
        GROUP BY categoria
    """
    
    df_estoque = pd.read_sql(query_filtrada, conn, params=(data_inicio, data_fim))

    if not df_estoque.empty:
        df_estoque['Preço Unit.'] = df_estoque['categoria'].map(CATEGORIAS_PRECOS).fillna(0)
        df_estoque['Total R$'] = df_estoque['saldo_qtd'] * df_estoque['Preço Unit.']
        
        total_patrimonial = df_estoque['Total R$'].sum()
        df_estoque['Part. %'] = (df_estoque['Total R$'] / total_patrimonial * 100) if total_patrimonial > 0 else 0
        df_grafico = df_estoque[df_estoque['Total R$'] > 0]

        total_cabecas = df_estoque['saldo_qtd'].sum()
        
        c1, c2, c3 = st.columns(3)
        c1.metric("Estoque Total", f"{total_cabecas} cab.")
        c2.metric("Valorização Total", f"R$ {total_patrimonial:,.2f}")
        c3.metric("Média por Animal", f"R$ {(total_patrimonial/total_cabecas if total_cabecas > 0 else 0):,.2f}")

        st.divider()

        col_graf, col_tab = st.columns([1, 1.2])

        with col_graf:
            st.subheader("Distribuição do Patrimônio (R$)")
            fig = px.pie(df_grafico, values='Total R$', names='categoria', hole=0.4,
                         color_discrete_sequence=px.colors.qualitative.Prism)
            fig.update_layout(legend=dict(orientation="h", yanchor="bottom", y=-0.2))
            st.plotly_chart(fig, use_container_width=True)

        with col_tab:
            st.subheader("Balanço e Participação")
            st.dataframe(df_estoque.style.format({
                'Preço Unit.': 'R$ {:.2f}', 'Total R$': 'R$ {:.2f}', 'Part. %': '{:.1f}%'
            }), use_container_width=True, hide_index=True)
            
            import io
            buffer = io.BytesIO()
            with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
                df_estoque.to_excel(writer, index=False, sheet_name='Balanco_AJAGRO')
            
            st.download_button(label="📥 Baixar Balanço (Excel)", data=buffer.getvalue(),
                               file_name=f"balanco_ajagro_{data_inicio}.xlsx",
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    else:
        st.info("Nenhum movimento encontrado no período selecionado.")

    st.divider()
    st.subheader("📑 Últimos 20 Lançamentos")
    query_hist = "SELECT strftime('%d/%m/%Y', data_movimento) as Data, tipo_movimento as Operação, categoria as Classe, quantidade as Qtd FROM lanc_estoque ORDER BY id_lancamento DESC LIMIT 20"
    df_hist = pd.read_sql(query_hist, conn)
    st.dataframe(df_hist, use_container_width=True, hide_index=True)
    conn.close()

# --- PÁGINA: AJUSTE DE PREÇOS (Nova Tela) ---
# AGORA ELA ESTÁ ALINHADA COM O ELIF ACIMA
elif menu == "⚙️ Ajuste de Preços":
    st.header("⚙️ Ajuste de Preços de Mercado")
    st.write("Altere os valores e salve para atualizar o cálculo patrimonial.")
    
    # Recarregamos para garantir que a tela mostre o que está no banco
    PRECOS_ATUAIS = carregar_precos()
    
    with st.form("form_precos"):
        st.subheader("Tabela de Valores Unitários")
        novos_precos = {}
        
        for cat, valor_atual in PRECOS_ATUAIS.items():
            novos_precos[cat] = st.number_input(f"{cat} (R$)", value=float(valor_atual), step=50.0)
        
        if st.form_submit_button("💾 Salvar Novos Preços"):
            conn = get_connection()
            for cat, valor in novos_precos.items():
                conn.execute("INSERT OR REPLACE INTO precos_gestao (categoria, valor) VALUES (?, ?)", (cat, valor))
            conn.commit()
            conn.close()
            st.success("Preços atualizados com sucesso!")
            st.balloons()
            st.rerun()
    


