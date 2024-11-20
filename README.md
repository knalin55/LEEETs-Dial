# LEEETs-Dial: Linguistic Entrainment in End-to-End Task-oriented Dialogue systems

The repository contains implementations of several methods (Instance Weighting, User Likelihood Loss & Conditioning on Lexical Keywords) to improve user-system dialogue alignment using GPT2-based model.
Check out `experiments/experiments.md` to learn more about the usage.
Don't forget to cite our paper if you use our work. 

```
@inproceedings{kumar-dusek-2024-leeets,
    title = "{LEEET}s-Dial: Linguistic Entrainment in End-to-End Task-oriented Dialogue systems",
    author = "Kumar, Nalin  and
      Dusek, Ondrej",
    editor = "Duh, Kevin  and
      Gomez, Helena  and
      Bethard, Steven",
    booktitle = "Findings of the Association for Computational Linguistics: NAACL 2024",
    month = jun,
    year = "2024",
    address = "Mexico City, Mexico",
    publisher = "Association for Computational Linguistics",
    url = "https://aclanthology.org/2024.findings-naacl.46",
    pages = "727--735",
    abstract = "Linguistic entrainment, or alignment, represents a phenomenon where linguistic patterns employed by conversational participants converge to one another. While entrainment has been shown to produce a more natural user experience, most dialogue systems do not have any provisions for it. In this work, we introduce methods for achieving dialogue entrainment in a GPT-2-based end-to-end task-oriented dialogue system through the utilization of shared vocabulary. We experiment with training instance weighting, entrainment-specific loss, and additional conditioning to generate responses that align with the user. We demonstrate that all three approaches produce significantly better entrainment than the base, non-entrainment-optimized model, as confirmed by both automated and manual evaluation metrics.",
}
```
[Link to the paper](https://aclanthology.org/2024.findings-naacl.46/)

The code is based on [AuGPT](https://github.com/ufal/augpt). To learn more about its installation and how to get started, please refer to the original repository at [AuGPT](https://github.com/ufal/augpt).

Please cite the AuGPT paper using

```
@inproceedings{kulhanek-etal-2021-augpt,
    title = "{AuGPT}: Auxiliary Tasks and Data Augmentation for End-To-End Dialogue with Pre-Trained Language Models",
    author = "Kulh{\'a}nek, Jon{\'a}{\v{s}}  and
      Hude{\v{c}}ek, Vojt{\v{e}}ch  and
      Nekvinda, Tom{\'a}{\v{s}}  and
      Du{\v{s}}ek, Ond{\v{r}}ej",
    editor = "Papangelis, Alexandros  and
      Budzianowski, Pawe{\l}  and
      Liu, Bing  and
      Nouri, Elnaz  and
      Rastogi, Abhinav  and
      Chen, Yun-Nung",
    booktitle = "Proceedings of the 3rd Workshop on Natural Language Processing for Conversational AI",
    month = nov,
    year = "2021",
    address = "Online",
    publisher = "Association for Computational Linguistics",
    url = "https://aclanthology.org/2021.nlp4convai-1.19",
    doi = "10.18653/v1/2021.nlp4convai-1.19",
    pages = "198--210",
    abstract = "Attention-based pre-trained language models such as GPT-2 brought considerable progress to end-to-end dialogue modelling. However, they also present considerable risks for task-oriented dialogue, such as lack of knowledge grounding or diversity. To address these issues, we introduce modified training objectives for language model finetuning, and we employ massive data augmentation via back-translation to increase the diversity of the training data. We further examine the possibilities of combining data from multiples sources to improve performance on the target dataset. We carefully evaluate our contributions with both human and automatic methods. Our model substantially outperforms the baseline on the MultiWOZ data and shows competitive performance with state of the art in both automatic and human evaluation.",
}
```

## Acknowledgements

This work was co-funded by the European Union (ERC, NG-NLG, 101039303) and HumanE-AI-Net project (EC Horizon 2020, Grant Agreement H2020-FETFLAG-2018-2020 no. 952026, micro-project “Use of dialog context to boost ASR/NLG/TTS and improve the overall quality of voice dialog systems”). The resources were provided by the LINDAT/CLARIAH-CZ Research Infrastructure (Czech Ministry of Education, Youth, and Sports project No. LM2018101). 




