#!/bin/bash
#SBATCH --job-name=PG_2006
#SBATCH --time=1-0:00:00
#SBATCH --mem=10G
module load python/3.7.4
srun python -u main_daily_run.py 2006
