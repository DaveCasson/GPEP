#!/bin/bash
#SBATCH --job-name=pop125
#SBATCH --time=0-14:00:00
#SBATCH --mem=15G
module load python/3.7.4
srun python -u temprun.py 12401 12501
