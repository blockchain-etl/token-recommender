# Copyright 2018 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""DAG definition for recserv model training."""

import airflow
from airflow import DAG
from airflow.contrib.operators.bigquery_operator import BigQueryOperator
from airflow.contrib.operators.bigquery_to_gcs import BigQueryToCloudStorageOperator
from airflow.contrib.operators.mlengine_operator import MLEngineTrainingOperator
from airflow.hooks.base_hook import BaseHook
from airflow.models import Variable

import datetime

from airflow.operators.bash_operator import BashOperator


def _get_project_id():
    """Get project ID from default GCP connection."""

    extras = BaseHook.get_connection('google_cloud_default').extra_dejson
    key = 'extra__google_cloud_platform__project'
    if key in extras:
        project_id = extras[key]
    else:
        raise ('Must configure project_id in google_cloud_default '
               'connection from Airflow Console')
    return project_id


PROJECT_ID = _get_project_id()

# GCS bucket names and region, can also be changed.
BUCKET = 'gs://recserve_' + PROJECT_ID
REGION = 'us-central1'

# The code package name comes from the model code in the wals_ml_engine
# directory of the solution code base.
PACKAGE_URI = BUCKET + '/code/ml_engine-0.1.tar.gz'
JOB_DIR = BUCKET + '/jobs'

print(airflow.version)

default_args = {
    'depends_on_past': False,
    'start_date': datetime.datetime(2018, 7, 1),
    'email': [Variable.get('notifications_email', 'airflow@example.com')],
    'email_on_failure': True,
    'email_on_retry': False,
    'retries': 5,
    'retry_delay': datetime.timedelta(minutes=5)
}

dag_train_model = DAG(
    'train_model',
    catchup=False,
    default_args=default_args,
    schedule_interval='0 0 * * *')

install_python2_command = \
        'cp /home/airflow/gcs/dags/resources/miniconda2.tar . && ' \
        'tar xvf miniconda2.tar > untar_miniconda.log && ' \
        'MINICONDA2=$PWD/miniconda2 && ' \
        'PYTHON2=$PWD/miniconda2/bin/python'

setup_command = \
        'set -o xtrace && set -o pipefail && ' + install_python2_command + \
        ' && ' \
        'git clone --branch master http://github.com/blockchain-etl/ethereum-scraper && ' \
        'cd ethereum-scraper/ethscraper && ' \
        'export CLOUDSDK_PYTHON=/usr/local/bin/python2'

crawl_tokens_command = \
        setup_command + ' && ' + \
        '$PYTHON2 $MINICONDA2/lib/python2.7/site-packages/scrapy/cmdline.py runspider spiders/etherscan_token_spider.py -o tokens.csv && ' \
        'if [[ $(wc -l <tokens.csv) -le 500 ]]; then echo "tokens.csv file has less than 500 lines" && exit 1; else echo "tokens.csv file ok"; fi && ' \
        'bq --location=US load --replace --source_format=CSV --skip_leading_rows=1 token_recommender.tokens tokens.csv /home/airflow/gcs/dags/resources/tokens_schema.json '



crawl_tokens_operator = BashOperator(
    task_id='crawl_tokens',
    bash_command=crawl_tokens_command,
    depends_on_past=False,
    execution_timeout=datetime.timedelta(hours=2),
    dag=dag_train_model
)

query_tokens_operator = BigQueryOperator(
    task_id='query_tokens',
    sql='''
#standardSQL
select eth_address as address, symbol, name from token_recommender.tokens as tokens
''',
    use_legacy_sql=False,
    destination_dataset_table='token_recommender.tokens_export',
    write_disposition='WRITE_TRUNCATE',
    dag=dag_train_model)

tokens_file = BUCKET + '/data/tokens.csv'
export_tokens_operator = BigQueryToCloudStorageOperator(
    task_id='export_tokens',
    source_project_dataset_table='token_recommender.tokens_export',
    destination_cloud_storage_uris=[tokens_file],
    export_format='CSV',
    dag=dag_train_model
)

# BigQuery training data query

sql = '''
#standardSQL
with tokens as (
  select *
  from token_recommender.tokens as tokens
  where true
    and tokens.symbol is not null
    and tokens.price is not null and tokens.price > 0
    and tokens.eth_address is not null
    and tokens.decimals is not null and tokens.decimals >= 0
),
token_balances as (
    with double_entry_book as (
        select token_address, to_address as address, cast(value as float64) as value, block_timestamp
        from `bigquery-public-data.ethereum_blockchain.token_transfers`
        union all
        select token_address, from_address as address, -cast(value as float64) as value, block_timestamp
        from `bigquery-public-data.ethereum_blockchain.token_transfers`
    )
    select double_entry_book.token_address, address, sum(value) as balance
    from double_entry_book
    join tokens on tokens.eth_address = double_entry_book.token_address
    where address != '0x0000000000000000000000000000000000000000'
    group by token_address, address
    having balance > 0
),
token_balances_usd as (
    select
        token_address,
        address,
        balance / pow(10, decimals) * price as balance
    from token_balances
    join tokens on tokens.eth_address = token_balances.token_address
),
filtered_token_balances_usd as (
    select *,
        count(1) over (partition by address) as token_count
    from token_balances_usd
    where balance >= 20
)
select
    token_address,
    address as user_address,
    balance as rating
from filtered_token_balances_usd
where true
    and token_count >= 2
'''

query_training_data_operator = BigQueryOperator(
    task_id='query_training_data',
    sql=sql,
    use_legacy_sql=False,
    destination_dataset_table='token_recommender.token_balances',
    write_disposition='WRITE_TRUNCATE',
    dag=dag_train_model)

# BigQuery training data export to GCS

training_file = BUCKET + '/data/token_balances.csv'
export_training_data_operator = BigQueryToCloudStorageOperator(
    task_id='export_training_data',
    source_project_dataset_table='token_recommender.token_balances',
    destination_cloud_storage_uris=[training_file],
    export_format='CSV',
    dag=dag_train_model
)

# ML Engine training job

job_id = 'recserve_{0}'.format(datetime.datetime.now().strftime('%Y%m%d%H%M'))
job_dir = BUCKET + '/jobs/' + job_id
output_dir = BUCKET
training_args = ['--job-dir', job_dir,
                 '--train-files', training_file,
                 '--output-dir', output_dir]

train_model_ml_engine_operator = MLEngineTrainingOperator(
    task_id='train_model_ml_engine',
    project_id=PROJECT_ID,
    job_id=job_id,
    package_uris=[PACKAGE_URI],
    training_python_module='trainer.task',
    training_args=training_args,
    python_version='3.5',
    runtime_version='1.4',
    region=REGION,
    scale_tier='BASIC',
    dag=dag_train_model
)


crawl_tokens_operator >> query_tokens_operator >> export_tokens_operator >> query_training_data_operator
query_training_data_operator >> export_training_data_operator
export_training_data_operator >> train_model_ml_engine_operator


# Adding t4 as downstream to t3 breaks the dag for some freaking unknown reason, so have to separate DAGs
dag_deploy_app_engine = DAG(
    'deploy_app_engine',
    catchup=False,
    default_args=default_args,
    schedule_interval='0 1 * * *')
# App Engine deploy new version

deploy_app_engine_version_operator = BashOperator(
    task_id='deploy_app_engine_version',
    bash_command='cd /home/airflow/gcs/dags/app && gcloud -q app deploy app_template.yaml_deploy.yaml',
    depends_on_past=False,
    dag=dag_deploy_app_engine
)
