## Implementation of experiments mentioned in the paper

# Base-CE

1.   Train using
```
python3 ./train_multiwoz.py  --train-dataset multiwoz-2.1-train --dev-dataset multiwoz-2.1-val --model jkulhanek/augpt-bigdata --epochs 10 --clean-samples  --dir_path=[CKPT_PATH] --response-loss ce  --save_best
```
2.   Generate using

```
python3  ./generate.py --dataset multiwoz-2.1-test --model [PATH_TO_CKPT] --file [FILENAME]

```

# Base-CE+Unl

1.   Train using
```
python3 ./train_multiwoz.py  --train-dataset multiwoz-2.1-train --dev-dataset multiwoz-2.1-val --model jkulhanek/augpt-bigdata --epochs 10 --clean-samples  --dir_path=[CKPT_PATH] --response-loss unlikelihood  --save_best
```
2.    Generation script similar to Base-CE

# IW1-CE

1.   Train using
```
python3 ./train_multiwoz.py  --train-dataset multiwoz-2.1-train --dev-dataset multiwoz-2.1-val --model jkulhanek/augpt-bigdata --epochs 10 --clean-samples  --dir_path=[CKPT_PATH] --response-loss ce --instance_weights simple  --save_best
```
2.    Generation script similar to Base-CE

# IW1-CE+Unl

1.   Train using
```
python3 ./train_multiwoz.py  --train-dataset multiwoz-2.1-train --dev-dataset multiwoz-2.1-val --model jkulhanek/augpt-bigdata --epochs 10 --clean-samples  --dir_path=[CKPT_PATH] --response-loss unlikelihood --instance_weights simple  --save_best
```
2.    Generation script similar to Base-CE

# IW2-CE

1.   Train using
```
python3 ./train_multiwoz.py  --train-dataset multiwoz-2.1-train --dev-dataset multiwoz-2.1-val --model jkulhanek/augpt-bigdata --epochs 10 --clean-samples  --dir_path=[CKPT_PATH] --response-loss ce --instance_weights mod_sigmoid  --save_best
```
2.    Generation script similar to Base-CE

# IW2-CE+Unl

1.   Train using
```
python3 ./train_multiwoz.py  --train-dataset multiwoz-2.1-train --dev-dataset multiwoz-2.1-val --model jkulhanek/augpt-bigdata --epochs 10 --clean-samples  --dir_path=[CKPT_PATH] --response-loss unlikelihood --instance_weights mod_sigmoid  --save_best
```
2.    Generation script similar to Base-CE


# ULL (ALPHA)

1.   Train using
```
python3 ./train_multiwoz.py  --train-dataset multiwoz-2.1-train --dev-dataset multiwoz-2.1-val --model jkulhanek/augpt-bigdata --epochs 10 --clean-samples  --dir_path=[CKPT_PATH] --rank_alpha_user [ALPHA] --response-loss user_overlap --include_unlikelihood   --save_best
```
2.    Generation script similar to Base-CE

# LK-CE-(SIGMA)

1.   Train using
```
python3 ./train_multiwoz.py  --train-dataset multiwoz-2.1-train --dev-dataset multiwoz-2.1-val --model jkulhanek/augpt-bigdata --epochs 10 --clean-samples  --dir_path=[CKPT_PATH] --response-loss ce --add_keyword lexicons-alpha_blending --alpha_blending [SIGMA]   --save_best
```
2.    Generate using
```
python3  ./generate.py --dataset multiwoz-2.1-test --model [PATH_TO_CKPT] --file [FILENAME] --add_keyword lexicons
```

# LK-CE+Unl-(SIGMA)

1.   Train using
```
python3 ./train_multiwoz.py  --train-dataset multiwoz-2.1-train --dev-dataset multiwoz-2.1-val --model jkulhanek/augpt-bigdata --epochs 10 --clean-samples  --dir_path=[CKPT_PATH] --response-loss unlikelihood --add_keyword lexicons-alpha_blending --alpha_blending [SIGMA]   --save_best
```
2.    Generate using
```
python3  ./generate.py --dataset multiwoz-2.1-test --model [PATH_TO_CKPT] --file [FILENAME] --add_keyword lexicons
```
